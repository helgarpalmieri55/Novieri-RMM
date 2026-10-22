/**
 * Device groups — the screen that lets an operator carve up a fleet at all.
 *
 * WHY IT MATTERS MORE THAN "ANOTHER CRUD PAGE". A group id is what a
 * deployment's `target_id` takes, what a patch, backup or remote policy keys
 * on, what a health profile assignment binds to, and what narrows a
 * `user_roles` RBAC grant. Until now nothing in the platform could create one,
 * so the only scope a console could express was the org default — which is
 * precisely the row that has no id.
 *
 * THE FIRST ROW IS NOT A GROUP. `ungrouped_device_count` renders as a fixed
 * row above the tree, because `device_group_id IS NULL` is not "no policy": it
 * is the ORG DEFAULT that patch, backup, remote and health all resolve to. The
 * audit's finding was that the console could name that scope and not size it.
 *
 * ORDER COMES FROM THE SERVER AND IS NEVER RE-SORTED HERE. The list arrives in
 * `(precedence, name)` — the same order `inventory/rules.py` walks to decide
 * which membership rule claims a device. `ORDER BY name` is evaluated in the
 * database's collation and `deploy/` sets none, so a client-side
 * `localeCompare` can disagree with the evaluator about which rule wins. The
 * tree below groups the server's SEQUENCE under parents; it does not sort.
 *
 * NO RAW JSON AND NO HAND-TYPED UUID. Recommendation 4 of the UX closeout was
 * "one error surface: no raw JSON in the UI" and recommendation 6 was
 * "EntityPicker: kill the hand-typed UUID". Parent is an `EntityPicker`; the
 * archive refusal is rendered as a list of places to go rather than as the
 * `blocked_by` object it arrives as; and the rules builder below is ROWS OF
 * SELECTS, never a textarea. A JSON textarea for `DeviceGroup.rules` would
 * have reintroduced both findings in a single control.
 *
 * THE RULES BUILDER IS AN AUTHORIZATION SURFACE WEARING A FORM. A group id is
 * what a deployment's `target_id` takes, so a rule that matches more machines
 * than its author intended is a way to send a signed job to machines nobody
 * chose. Two things follow, and both are load-bearing rather than polish:
 *
 *   1. THE FACT LIST IS A `<select>`, so a typo is unreachable rather than a
 *      422. That matters more than it sounds: an unknown fact path resolves to
 *      "missing" in the evaluator, and "is not" HOLDS on a missing fact — so
 *      `os_familly is not windows` claims every device in the organisation and
 *      reads like a reasonable rule. The server refuses it too; this is what
 *      stops it being typed.
 *   2. THE REACH IS SHOWN BEFORE THE SAVE. Every settled edit calls
 *      `POST /device-groups/preview`, which writes nothing and answers the
 *      exact count, the count of devices no rule can match, and a sample naming
 *      the group each one is in NOW. Membership is applied by a background
 *      sweep, so nothing moves at the moment of saving — which is exactly why
 *      the author has to see the set before they save rather than after.
 *
 * MEMBERSHIP IS HAND-PICKED OR RULE-DRIVEN, NEVER BOTH, and the form says so
 * with a control rather than a warning. A rule CLAIMS a device rather than
 * joining it, so a hand placement inside a rule-driven group is an edit the
 * next re-evaluation reverts with no message.
 *
 * EVERY CONTROL IS GATED ON `devices.manage`, which is presentation only:
 * the server decides, always (invariant 6). Hiding a control the operator
 * cannot use is a courtesy; the 403 is the security.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { Trans } from "../../i18n/Trans";
import { useT } from "../../i18n/context";
import { translate, type MessageKey } from "../../i18n/core";

import { ConfirmDialog } from "../../components/confirm";
import { Prerequisites } from "../../components/prerequisites";
import { EmptyState } from "../../components/emptystate";
import {
  Form,
  FormActions,
  attemptProblems,
  describeSubmitError,
  type Attempt,
} from "../../components/form";
import { EntityPicker } from "../../components/picker";
import { NoValue } from "../../components/emptystate";
import { Button, Dialog, Field, Td, Th, inputClass } from "../../components/ui";
import type {
  Device,
  DeviceGroup,
  DeviceGroupReferences,
  GroupRules,
  Me,
  MembershipEvaluation,
  RuleCondition,
  RuleOp,
  RulePreview,
} from "../../lib/api";
import { ApiError } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { formatDateTime } from "../../lib/datetime";
import { useFleetPrerequisite } from "../../lib/detectors";
import { PageHead, Panel, QueryState, StatusPill, TableWrap } from "../../shell/components";
import { Icon } from "../../shell/icons";

/**
 * How stale the last sweep may be before the page stops reassuring and starts
 * warning: two beats.
 *
 * ONE MISSED BEAT IS NOT A FAULT. The worker is a Celery beat; a run that
 * overlaps a slow one, a restart, a redeploy all cost a single interval and
 * mean nothing. TWO consecutive misses mean the sweep is not running, and that
 * IS the finding the page exists to make visible — the design's cycle-2 note
 * names it exactly: the screen "cannot distinguish 'swept four minutes ago'
 * from 'the worker has been down since Tuesday'".
 *
 * A MULTIPLE OF THE SERVER'S OWN INTERVAL, never a literal number of minutes.
 * `interval_seconds` comes off the response and is the beat's own constant, so
 * changing the schedule changes this threshold with it.
 */
const STALE_INTERVALS = 2;

/**
 * "4 minutes ago", or a date once that stops being the useful unit.
 *
 * NOT A LIBRARY AND NOT `Intl.RelativeTimeFormat` WITH A GUESSED UNIT. The
 * only question this line answers is "did the sweep run recently", and the
 * beat is 300 seconds — so minutes are the resolution that matters and an
 * hour is already the answer "no". Past a day it degrades to the absolute
 * timestamp, because "17 days ago" is a number nobody converts back.
 */
function describeAge(iso: string, now: number, zone?: string | null): string {
  const seconds = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (seconds < 90) return translate("time.justNow");
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return translate("time.ago.minute", { count: minutes });
  const hours = Math.round(minutes / 60);
  if (hours < 24) return translate("time.ago.hour", { count: hours });
  return formatDateTime(iso, zone);
}

/**
 * The one sentence under the table, and what it may not claim.
 *
 * THREE STATES, NOT TWO. `null` is "no sweep has ever run for this
 * organization", which is a different sentence from "the last one was a while
 * ago" and must never render as "just now" — a page that cannot distinguish
 * them is the page this whole record was added to fix.
 */
function describeEvaluation(
  evaluation: MembershipEvaluation | null,
  now: number,
  zone?: string | null,
): { text: string; stale: boolean } {
  if (evaluation === null) {
    return {
      text: translate("devices.groups.evaluation.never"),
      stale: true,
    };
  }
  const age = now - new Date(evaluation.last_evaluated_at).getTime();
  const stale = age > evaluation.interval_seconds * STALE_INTERVALS * 1000;
  const when = describeAge(evaluation.last_evaluated_at, now, zone);
  const scope = translate(
    evaluation.full_pass ? "devices.groups.evaluation.scopeFull" : "devices.groups.evaluation.scopeChanged",
    { count: evaluation.devices_evaluated },
  );
  const moved = translate("devices.groups.evaluation.moved", { count: evaluation.devices_moved });
  const blind =
    evaluation.devices_without_inventory > 0
      ? translate("devices.groups.evaluation.blind", { count: evaluation.devices_without_inventory })
      : "";
  if (stale) {
    return {
      text: translate("devices.groups.evaluation.stale", {
        when,
        minutes: Math.round(evaluation.interval_seconds / 60),
      }),
      stale: true,
    };
  }
  return {
    text: translate("devices.groups.evaluation.fresh", { when, scope, moved, blind }),
    stale: false,
  };
}

/**
 * Each blocker, in the operator's words and with the screen that owns it.
 *
 * A REFUSAL THE OPERATOR CANNOT ACT ON IS A DEAD END; this one is a to-do
 * list. `child_groups` has no `to` because the thing holding the group is on
 * this very page.
 */
const BLOCKERS: readonly {
  key: keyof DeviceGroupReferences;
  to?: string;
}[] = [
  { key: "devices", to: "/devices" },
  { key: "child_groups" },
  { key: "policies", to: "/patch" },
  { key: "patch_policies", to: "/patch" },
  { key: "backup_policies", to: "/backup" },
  { key: "remote_policies", to: "/remote" },
  { key: "health_assignments", to: "/health" },
  { key: "credentials", to: "/credentials" },
  { key: "role_grants", to: "/settings" },
];

/** One blocker as a phrase, read when drawn so it follows the language. */
function blockerLabel(key: keyof DeviceGroupReferences, n: number): string {
  return translate(`devices.groups.blockers.${key}` as MessageKey, { count: n });
}

/**
 * The `blocked_by` object out of a 409, or null when this was some other
 * failure.
 *
 * The body arrives as text on `ApiError.message` and is parsed HERE rather
 * than shown: a JSON blob on screen is the defect the UX programme spent a
 * cycle closing.
 */
export function parseBlockedBy(error: unknown): DeviceGroupReferences | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  try {
    const body: unknown = JSON.parse(error.message);
    if (typeof body !== "object" || body === null) return null;
    const blocked = (body as { blocked_by?: unknown }).blocked_by;
    if (typeof blocked !== "object" || blocked === null) return null;
    return blocked as DeviceGroupReferences;
  } catch {
    return null;
  }
}

/**
 * The server's sequence, nested under parents, WITHOUT re-sorting.
 *
 * Children are collected in the order the server returned them, so the tree
 * renders `(precedence, name)` at every level. A group whose parent is not in
 * the current page (an archived parent, say) is rendered at the top level
 * rather than dropped — a row that vanishes is worse than a row shown one
 * level too high.
 */
export function buildTree(
  groups: readonly DeviceGroup[],
): { group: DeviceGroup; depth: number }[] {
  const present = new Set(groups.map((group) => group.id));
  const children = new Map<string | null, DeviceGroup[]>();
  for (const group of groups) {
    const parent =
      group.parent_group_id && present.has(group.parent_group_id)
        ? group.parent_group_id
        : null;
    const bucket = children.get(parent);
    if (bucket) bucket.push(group);
    else children.set(parent, [group]);
  }

  const rows: { group: DeviceGroup; depth: number }[] = [];
  const walk = (parent: string | null, depth: number) => {
    for (const group of children.get(parent) ?? []) {
      rows.push({ group, depth });
      walk(group.id, depth + 1);
    }
  };
  walk(null, 0);
  return rows;
}

/**
 * THE CLOSED FACT NAMESPACE, in the operator's words.
 *
 * The keys are exactly `app/modules/inventory/rules.py::FLAT_FACTS` and the
 * evaluator can read nothing else without a `facts.` or `software:` prefix.
 * Because the list is closed, this control can be exhaustive — and a typo
 * becomes UNREACHABLE rather than a 422. The server refuses an unknown path
 * too; this is the half that stops one being typed in the first place.
 *
 * IF THE TWO LISTS DRIFT, THE FAILURE IS BENIGN IN ONE DIRECTION AND VISIBLE
 * IN THE OTHER. A fact missing here is a rule the operator cannot express; an
 * extra one is a 422 with the fact named. Neither is a way to match more
 * machines than intended, which is why this is a constant rather than a route.
 */
const FLAT_FACTS = ["hostname", "os_family", "os_version", "architecture", "ram_bytes", "serial"] as const;

/** The two open-ended prefixes, each revealing a second labelled input. */
const CUSTOM_FACT = "facts.";
const SOFTWARE_FACT = "software:";

/**
 * The five comparisons, LABELLED IN WORDS.
 *
 * "is not" is the one worth reading twice: it is the only op that HOLDS when
 * the fact is missing from a device's inventory document. On a fact path the
 * evaluator does not know, that means it matches every device in the
 * organisation — which is why the fact control above is a select.
 */
const OPS: readonly RuleOp[] = ["eq", "ne", "contains", "gt", "lt"];

/**
 * Facts the agent reports as a real boolean, so the control is a checkbox and
 * the value sent is `true`, not `"true"`.
 *
 * IT IS A TYPE MATCH, NOT A NICETY. The evaluator compares with `==`, so
 * `facts.domain_joined is "true"` matches NOTHING while looking exactly right
 * — a rule that quietly claims an empty fleet.
 */
const BOOLEAN_FACTS: readonly string[] = [
  "facts.domain_joined",
  "facts.disk_encryption",
  "facts.firewall_enabled",
];

/**
 * Facts the agent reports as a number, for the same reason and with the same
 * failure: `ram_bytes is "17179869184"` compares a string to an int and
 * matches nothing.
 */
const NUMERIC_FACTS: readonly string[] = ["ram_bytes"];

export type FactKind = "flat" | "custom" | "software";

/** One row of the builder, before it becomes a condition. */
export interface ConditionDraft {
  kind: FactKind;
  /** A flat fact path, or the remainder after `facts.` / `software:`. */
  base: string;
  op: RuleOp;
  /** Always a string in the form; coerced on the way out — see `toDocument`. */
  value: string;
}

export function blankCondition(): ConditionDraft {
  return { kind: "flat", base: "os_family", op: "eq", value: "" };
}

/** The fact path this row names, or "" while a prefix row has no remainder. */
export function factPath(draft: ConditionDraft): string {
  if (draft.kind === "flat") return draft.base;
  const base = draft.base.trim();
  if (!base) return "";
  return draft.kind === "custom" ? `${CUSTOM_FACT}${base}` : `${SOFTWARE_FACT}${base}`;
}

/** Should this row's value be a checkbox, a number box, or a text box? */
export function valueControl(draft: ConditionDraft): "boolean" | "number" | "text" {
  if (draft.op === "gt" || draft.op === "lt") return "number";
  if (BOOLEAN_FACTS.includes(factPath(draft))) return "boolean";
  return "text";
}

/**
 * The rules document these rows describe, or null when any row is incomplete.
 *
 * THE COERCION IS THE POINT. `<input>` yields strings and the evaluator
 * compares with `==` against whatever the agent collected, so a numeric fact
 * compared to a string matches nothing at all — a rule that looks right and
 * claims an empty fleet. `gt`/`lt` always send a number because the evaluator
 * calls `float()` on both sides; a known boolean fact sends a boolean; a known
 * numeric fact sends a number when the text parses as one.
 *
 * Null rather than a partial document, because a half-typed `facts.` path is
 * a 422 and there is nothing to preview yet.
 */
export function toDocument(drafts: readonly ConditionDraft[]): GroupRules | null {
  if (drafts.length === 0) return null;
  const all: RuleCondition[] = [];
  for (const draft of drafts) {
    const fact = factPath(draft);
    if (!fact) return null;
    const control = valueControl(draft);
    if (control === "boolean") {
      all.push({ fact, op: draft.op, value: draft.value === "true" });
      continue;
    }
    const text = draft.value.trim();
    if (!text) return null;
    if (control === "number") {
      const parsed = Number(text);
      if (!Number.isFinite(parsed)) return null;
      all.push({ fact, op: draft.op, value: parsed });
      continue;
    }
    const numeric = NUMERIC_FACTS.includes(fact) && Number.isFinite(Number(text));
    all.push({ fact, op: draft.op, value: numeric ? Number(text) : text });
  }
  return { all };
}

/** The stored document, back into rows the builder can edit. */
export function fromDocument(rules: GroupRules | null): ConditionDraft[] {
  if (!rules) return [];
  return rules.all.map((condition) => {
    const value = String(condition.value);
    if (condition.fact.startsWith(CUSTOM_FACT)) {
      return {
        kind: "custom" as const,
        base: condition.fact.slice(CUSTOM_FACT.length),
        op: condition.op,
        value,
      };
    }
    if (condition.fact.startsWith(SOFTWARE_FACT)) {
      return {
        kind: "software" as const,
        base: condition.fact.slice(SOFTWARE_FACT.length),
        op: condition.op,
        value,
      };
    }
    return { kind: "flat" as const, base: condition.fact, op: condition.op, value };
  });
}

/**
 * What the preview says, in two sentences.
 *
 * THE SECOND SENTENCE IS NOT DECORATION. The evaluator reads collected
 * inventory DOCUMENTS and never device rows, so a manually-created asset, a
 * discovered switch and an enrolled machine that has not collected yet can
 * never be claimed by any rule. An operator who writes a rule for a fleet,
 * receives half of it, and is not told why concludes the rule is wrong.
 *
 * The denominator is the inventory's device count. `matched` is exact; the
 * total is what `GET /devices` reports, which is every device rather than only
 * the active ones the sweep walks — so the fraction can read generously on a
 * fleet holding decommissioned machines. The number that decides anything is
 * the numerator, and it is the server's.
 */
export function previewSentence(preview: RulePreview, fleet: number): string {
  const matched = translate("devices.groups.preview.matches", { matched: preview.matched, count: fleet });
  if (preview.devices_without_inventory === 0) return matched;
  const n = preview.devices_without_inventory;
  return `${matched} ${translate("devices.groups.preview.blind", { count: n })}`;
}

interface EditorState {
  /** Null while creating. */
  group: DeviceGroup | null;
  name: string;
  description: string;
  precedence: string;
  parentGroupId: string;
  /** Hand-picked or rule-driven. Never both — see the module docstring. */
  dynamic: boolean;
  conditions: ConditionDraft[];
}

function blankEditor(): EditorState {
  return {
    group: null,
    name: "",
    description: "",
    precedence: "0",
    parentGroupId: "",
    dynamic: false,
    conditions: [],
  };
}

export function GroupsPage() {
  const t = useT();
  const api = useApi();
  // A group holds computers; with none in the inventory there is nothing to
  // put in one (docs/design/cross-module-guidance.md §2, Device groups).
  const computersItem = useFleetPrerequisite("asset", {
    missingDetail: t("devices.groups.noComputers"),
  });
  const queryClient = useQueryClient();

  const [showArchived, setShowArchived] = useState(false);
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  // The group the operator has ASKED to archive, not one being archived:
  // nothing is sent until the confirmation is answered.
  const [pendingArchive, setPendingArchive] = useState<DeviceGroup | null>(null);
  const [blocked, setBlocked] = useState<
    { group: DeviceGroup; references: DeviceGroupReferences } | null
  >(null);
  const [openGroup, setOpenGroup] = useState<DeviceGroup | null>(null);
  const [addingDevice, setAddingDevice] = useState("");
  const [loadingRules, setLoadingRules] = useState(false);

  const me = useQuery<Me>({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 60_000 });
  const canManage = me.data?.permissions.includes("devices.manage") ?? false;

  const groups = useQuery({
    queryKey: ["device-groups", showArchived],
    queryFn: () => api.listDeviceGroups({ include_archived: showArchived }),
  });
  const devices = useQuery({ queryKey: ["devices"], queryFn: () => api.listDevices() });

  const items = useMemo(() => groups.data?.items ?? [], [groups.data]);
  const rows = useMemo(() => buildTree(items), [items]);
  /**
   * The parent's NAME, for the row that is drawn inside it.
   *
   * `buildTree` already refuses a parent that is not on this page (an archived
   * one, say) and renders that group at the top level; this reads the same
   * list, so a row that is not indented is never described as nested either.
   * The two must agree, which is why this looks the id up in `items` rather
   * than trusting `parent_group_id` on its own.
   */
  const parentName = useMemo(() => {
    const byId = new Map(items.map((group) => [group.id, group.name]));
    return (group: DeviceGroup): string | null =>
      group.parent_group_id ? (byId.get(group.parent_group_id) ?? null) : null;
  }, [items]);
  /**
   * Read ONCE PER RENDER, not per row, so every relative age on the screen is
   * measured from the same instant. Two `Date.now()` calls a millisecond apart
   * can straddle a minute boundary and print two different answers for one
   * fact.
   */
  const now = Date.now();
  const evaluation = groups.data ? groups.data.evaluation : undefined;

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["device-groups"] });
    void queryClient.invalidateQueries({ queryKey: ["devices"] });
  };

  const save = useMutation({
    mutationFn: (state: EditorState) => {
      const body = {
        name: state.name.trim(),
        description: state.description.trim(),
        parent_group_id: state.parentGroupId === "" ? null : state.parentGroupId,
        precedence: Number(state.precedence),
        // `null` CLEARS the document and moves nobody: a rule only ever claims
        // a device, it never releases one, so the members stay put and the
        // group becomes hand-picked holding exactly the same machines. That is
        // what lets a rule-driven group be emptied, and therefore archived.
        rules: state.dynamic ? toDocument(state.conditions) : null,
      };
      return state.group
        ? api.updateDeviceGroup(state.group.id, body)
        : api.createDeviceGroup(body);
    },
    onSuccess: () => {
      setEditor(null);
      refresh();
    },
  });

  const archive = useMutation({
    mutationFn: (group: DeviceGroup) => api.archiveDeviceGroup(group.id),
    onSuccess: () => {
      setPendingArchive(null);
      refresh();
    },
    onError: (error, group) => {
      const references = parseBlockedBy(error);
      setPendingArchive(null);
      if (references) setBlocked({ group, references });
    },
  });

  const unarchive = useMutation({
    mutationFn: (group: DeviceGroup) => api.unarchiveDeviceGroup(group.id),
    onSuccess: refresh,
  });

  const addMember = useMutation({
    mutationFn: (payload: { groupId: string; deviceId: string }) =>
      api.addDeviceGroupMembers(payload.groupId, [payload.deviceId]),
    onSuccess: () => {
      setAddingDevice("");
      refresh();
    },
  });

  const removeMember = useMutation({
    mutationFn: (payload: { groupId: string; deviceId: string }) =>
      api.removeDeviceGroupMember(payload.groupId, payload.deviceId),
    onSuccess: refresh,
  });

  function openCreate() {
    save.reset();
    setAttempt(null);
    setEditor(blankEditor());
  }

  /**
   * Open the editor on an existing group, FETCHING ITS RULES FIRST.
   *
   * The paged list deliberately does not carry the document — `787461f` kept
   * it off because a per-row document is a different decision about response
   * size — so the detail route is the only place it exists. Opening the form
   * without it would present a rule-driven group as if it had no rules, and
   * saving would then clear them.
   */
  async function openEdit(group: DeviceGroup) {
    save.reset();
    setAttempt(null);
    setLoadingRules(true);
    try {
      const detail = await api.getDeviceGroup(group.id);
      setEditor({
        group,
        name: group.name,
        description: group.description,
        precedence: String(group.precedence),
        parentGroupId: group.parent_group_id ?? "",
        dynamic: detail.rules !== null,
        conditions: fromDocument(detail.rules),
      });
    } finally {
      setLoadingRules(false);
    }
  }

  // ------------------------------------------------------------- the preview
  //
  // DEBOUNCED, BECAUSE "EVERY SETTLED EDIT" IS THE SPEC AND EVERY KEYSTROKE IS
  // NOT. `/preview` walks the org's active devices and reads each one's latest
  // inventory document; firing it per character would put a fleet-sized read
  // behind a text box. The document is serialised into the query key, so
  // TanStack caches identical rule shapes and a round trip back to a rule the
  // author already saw costs nothing.
  const document = editor?.dynamic ? toDocument(editor.conditions) : null;
  const documentKey = document ? JSON.stringify(document) : null;
  const [settled, setSettled] = useState<string | null>(null);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(documentKey), 400);
    return () => clearTimeout(timer);
  }, [documentKey]);

  const preview = useQuery({
    queryKey: ["device-group-preview", settled],
    queryFn: () =>
      api.previewDeviceGroupRules(
        JSON.parse(settled ?? "null") as GroupRules,
        editor?.group?.id ?? null,
      ),
    enabled: settled !== null && canManage,
    // A preview is a statement about the fleet as it is now; a cached answer
    // from ten minutes ago is a different claim.
    staleTime: 30_000,
  });

  const fleetSize = devices.data?.total ?? 0;

  function setCondition(index: number, next: Partial<ConditionDraft>) {
    if (!editor) return;
    setEditor({
      ...editor,
      conditions: editor.conditions.map((condition, at) =>
        at === index ? { ...condition, ...next } : condition,
      ),
    });
  }

  function submit() {
    if (!editor) return;
    // Held as rules + values, not as sentences, so a language switch rewords them.
    const next: Attempt = {
      rules: () => [
        { name: "name", label: translate("devices.groups.editor.name") },
        {
          name: "precedence",
          label: translate("devices.groups.editor.precedence"),
          validate: (value) =>
            Number.isInteger(Number(value)) && Math.abs(Number(value)) <= 32767
              ? null
              : translate("devices.groups.editor.precedenceInvalid"),
        },
      ],
      values: { name: editor.name, precedence: editor.precedence },
    };
    setAttempt(next);
    if (attemptProblems(next).length > 0) return;
    save.mutate(editor);
  }

  // The parent options exclude the group being edited: a group cannot be its
  // own parent, and offering the choice would be offering a 422.
  const parentOptions = items
    .filter((group) => group.archived_at === null && group.id !== editor?.group?.id)
    .map((group) => ({
      id: group.id,
      label: group.name,
      sublabel: t("devices.groups.editor.parentSub", {
        precedence: group.precedence,
        count: group.device_count,
      }),
    }));

  const deviceRows = devices.data?.items ?? [];
  const members = openGroup
    ? deviceRows.filter((device) => device.device_group_id === openGroup.id)
    : [];
  const joinable = openGroup
    ? deviceRows.filter((device) => device.device_group_id !== openGroup.id)
    : [];

  return (
    <>
      <PageHead
        title={t("devices.groups.title")}
        description={t("devices.groups.description")}
        actions={
          canManage ? (
            <Button onClick={openCreate}>
              <Icon name="plus" size={14} />
              <span>{t("devices.groups.newGroup")}</span>
            </Button>
          ) : undefined
        }
      />

      <Prerequisites title={t("devices.groups.prerequisitesTitle")} items={[computersItem]} />

      <Panel
        title={t("devices.groups.panelTitle")}
        subtitle={t("devices.groups.panelSubtitle")}
        bare
        actions={
          <label className="flex items-center gap-2" style={{ whiteSpace: "nowrap" }}>
            <input
              type="checkbox"
              checked={showArchived}
              onChange={(event) => setShowArchived(event.target.checked)}
            />
            <span>{t("devices.groups.showArchived")}</span>
          </label>
        }
      >
        {/* `empty` IS ALWAYS FALSE HERE, AND THAT IS DELIBERATE. The table
            still has a row to render when this org has no groups at all --
            the organisation default, which is the widest scope on the screen
            and the one that has no id. `QueryState` is used for the two
            states a table body genuinely cannot express, loading and failed;
            the "no groups yet" case is an `EmptyState` BELOW the table, so
            the default's size stays on screen while it is explained. */}
        {/* A TIMESTAMP, NEVER ALONE — and never invented.
            `docs/superpowers/specs/2026-08-22-device-group-management-design.md`
            §5.3 and §9 asked for this and cycle 2 could not deliver it: the
            sweep's only durable record was an audit event, and reading it
            needs `audit.read`, which this page's `devices.read` gate does not
            entitle. So the panel subtitle stated the INTERVAL instead — true,
            and unable to tell "swept four minutes ago" from "the worker has
            been down since Tuesday". `evaluation` on the list response is that
            record; the sentence below is what it buys.

            RENDERED ONLY WHEN THE LIST HAS ACTUALLY LOADED. "Never
            re-evaluated" is a claim about data, and asserting it beside a
            spinner or a failed request would be asserting it from a screen
            that has not been told. */}
        {evaluation !== undefined
          ? (() => {
              const described = describeEvaluation(evaluation, now, me.data?.timezone);
              return (
                <p
                  role={described.stale ? "alert" : "status"}
                  className={described.stale ? "notice gold" : "note"}
                  style={{ margin: "12px 18px" }}
                >
                  {described.text}
                </p>
              );
            })()
          : null}
        <QueryState
          loading={groups.isLoading}
          error={groups.error}
          empty={false}
          emptyLabel={t("devices.groups.emptyLabel")}
          emptyIcon="network"
        >
          <TableWrap>
            <table>
              <thead>
                <tr>
                  <th scope="col">{t("devices.groups.colGroup")}</th>
                  <th scope="col">{t("devices.groups.colPrecedence")}</th>
                  <th scope="col">{t("devices.groups.colDevices")}</th>
                  <th scope="col">{t("devices.groups.colMembership")}</th>
                  <Th />
                </tr>
              </thead>
              <tbody>
                {/* NOT A GROUP, AND DELIBERATELY FIRST. Every policy table
                    reads `device_group_id IS NULL` as the organisation
                    default, so this row is the scope with the widest reach and
                    the only one that has no id. */}
                <tr>
                  <Td lead={<Icon name="building" size={18} />}>
                    <div className="cell-title">{t("devices.groups.noGroup")}</div>
                    <div className="cell-sub">{t("devices.groups.noGroupSub")}</div>
                  </Td>
                  <td>
                    <NoValue label={t("devices.groups.noPrecedence")} />
                  </td>
                  <td className="mono">{groups.data?.ungrouped_device_count ?? 0}</td>
                  <td>
                    <NoValue label={t("devices.groups.notAGroup")} />
                  </td>
                  <td>{""}</td>
                </tr>

                {rows.map(({ group, depth }) => (
                  <tr key={group.id}>
                    {/* v3's row: the building glyph leads, the name is the
                        title, the rule (or "Manual membership") the sub-line.

                        THE INDENT IS ON THE CELL, AND THAT IS THE WHOLE FIX
                        (design review I4). It used to be on the two text divs
                        INSIDE the cell, so a child's glyph stayed at its
                        parent's x while its words stepped 18px right. The
                        review found exactly that on `Build runners` — whose
                        `parent_group_id` really is `Production servers`, so
                        the offset was never a bug in the arithmetic — and read
                        it as a ragged column rather than as nesting, because
                        an indent that leaves the row's leading glyph behind is
                        not a nesting cue: it is one element out of line.
                        Indenting the CELL moves glyph and text together, which
                        is the step a tree draws.

                        AND THE NESTING IS SAID IN WORDS TOO. A depth rendered
                        only as whitespace is invisible to a screen reader and
                        to anyone who cannot see 18px of it; `aria-level`
                        belongs to a `treegrid`, which this table is not, so
                        the parent is named in an `.sr-only` line instead. That
                        is the second cue the review said was missing.

                        12px IS `td`'s OWN PADDING (`base.css`), added back
                        because `paddingLeft` replaces it rather than adding to
                        it. Depth 0 states no style at all, so an unnested
                        table is byte-for-byte what it was. */}
                    <Td
                      lead={<Icon name="building" size={18} />}
                      style={depth > 0 ? { paddingLeft: `${12 + depth * 18}px` } : undefined}
                    >
                      <div className="cell-title">
                        {group.name}
                        {parentName(group) ? (
                          <span className="sr-only">{t("devices.groups.insideParent", { parent: parentName(group) ?? "" })}</span>
                        ) : null}
                        {group.archived_at ? (
                          <>
                            {" "}
                            <StatusPill value="archived" label={t("devices.groups.archivedPill")} />
                          </>
                        ) : null}
                      </div>
                      {group.description ? (
                        <div className="cell-sub">{group.description}</div>
                      ) : null}
                    </Td>
                    <td className="mono">{group.precedence}</td>
                    <td className="mono">{group.device_count}</td>
                    <td>
                      {group.dynamic ? (
                        <StatusPill value="rule-driven" kind="info" label={t("devices.groups.ruleDriven")} />
                      ) : (
                        <StatusPill value="hand-picked" kind="mut" label={t("devices.groups.handPicked")} />
                      )}
                    </td>
                    <td>
                      <div className="flex gap-2 justify-end">
                        {group.archived_at === null ? (
                          <Button
                            variant="ghost"
                            onClick={() => {
                              setOpenGroup(group);
                              setAddingDevice("");
                              addMember.reset();
                              removeMember.reset();
                            }}
                          >
                            {t("devices.groups.members")}
                          </Button>
                        ) : null}
                        {canManage && group.archived_at === null ? (
                          <>
                            <Button
                              variant="ghost"
                              disabled={loadingRules}
                              onClick={() => void openEdit(group)}
                            >
                              {t("devices.groups.edit")}
                            </Button>
                            <Button
                              variant="ghost"
                              onClick={() => {
                                archive.reset();
                                setBlocked(null);
                                setPendingArchive(group);
                              }}
                            >
                              {t("devices.groups.archive")}
                            </Button>
                          </>
                        ) : null}
                        {canManage && group.archived_at !== null ? (
                          <Button variant="ghost" onClick={() => unarchive.mutate(group)}>
                            {t("devices.groups.unarchive")}
                          </Button>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>

          {items.length === 0 ? (
            <EmptyState
              icon="network"
              title={t("devices.groups.emptyTitle")}
              body={t("devices.groups.emptyBody")}
              action={canManage ? <Button onClick={openCreate}>{t("devices.groups.newGroup")}</Button> : undefined}
            />
          ) : null}
        </QueryState>
      </Panel>

      {unarchive.isError ? (
        <p role="alert" className="form-error mt-3">
          {describeSubmitError(unarchive.error)}
        </p>
      ) : null}

      {/* ------------------------------------------------------------ editor */}

      <Dialog
        open={editor !== null}
        onClose={() => setEditor(null)}
        title={editor?.group ? t("devices.groups.editor.editTitle", { name: editor.group.name }) : t("devices.groups.editor.newTitle")}
        dismissible={false}
      >
        {editor ? (
          <Form onSubmit={submit}>
            <div className="form-grid">
              <Field label={t("devices.groups.editor.name")}>
                <input
                  className={inputClass}
                  value={editor.name}
                  onChange={(event) =>
                    setEditor({ ...editor, name: event.target.value })
                  }
                />
              </Field>
              <Field label={t("devices.groups.editor.description")}>
                <input
                  className={inputClass}
                  value={editor.description}
                  onChange={(event) =>
                    setEditor({ ...editor, description: event.target.value })
                  }
                />
              </Field>
              {/* A NUMBER, AND THE HELP TEXT SAYS WHAT IT DECIDES. This is what
                  stops a rename from moving machines: before it existed, the
                  alphabet was the precedence list. */}
              <Field label={t("devices.groups.editor.precedence")}>
                <input
                  className={inputClass}
                  type="number"
                  value={editor.precedence}
                  onChange={(event) =>
                    setEditor({ ...editor, precedence: event.target.value })
                  }
                />
              </Field>
              <p className="note">
                {t("devices.groups.editor.precedenceNote")}
              </p>
              {/* A PICKER, NEVER A TYPED UUID (closeout rec. 6). Health profile
                  resolution walks this chain upward; a deployment does not
                  descend it. */}
              <EntityPicker
                label={t("devices.groups.editor.parent")}
                options={parentOptions}
                value={editor.parentGroupId}
                onChange={(id) => setEditor({ ...editor, parentGroupId: id })}
                emptyHint={t("devices.groups.editor.parentEmpty")}
              />

              {/* ------------------------------------------------ membership */}
              {/* ONE CONTROL, NOT A WARNING. A rule CLAIMS a device rather
                  than joining it, so a group cannot be both hand-picked and
                  rule-driven: the next re-evaluation would revert the hand
                  placements with no message. The server refuses it too. */}
              <Field label={t("devices.groups.editor.membership")} full>
                <select
                  className={inputClass}
                  value={editor.dynamic ? "dynamic" : "static"}
                  onChange={(event) =>
                    setEditor({
                      ...editor,
                      dynamic: event.target.value === "dynamic",
                      conditions:
                        event.target.value === "dynamic" && editor.conditions.length === 0
                          ? [blankCondition()]
                          : editor.conditions,
                    })
                  }
                >
                  <option value="static">{t("devices.groups.editor.static")}</option>
                  <option value="dynamic">{t("devices.groups.editor.dynamic")}</option>
                </select>
              </Field>

              {editor.dynamic && (editor.group?.device_count ?? 0) > 0 ? (
                <p className="notice">
                  {t("devices.groups.editor.alreadyHolds", { count: editor.group?.device_count ?? 0 })}
                </p>
              ) : null}

              {editor.dynamic ? (
                <>
                  <p className="note">
                    <Trans
                      k="devices.groups.editor.rulesIntro"
                      vars={{
                        pass: evaluation
                          ? t("devices.groups.editor.passWithAge", {
                              when: describeAge(evaluation.last_evaluated_at, now, me.data?.timezone),
                            })
                          : t("devices.groups.editor.passUnknown"),
                      }}
                      components={{ strong: <strong /> }}
                    />
                  </p>
                  {editor.conditions.map((condition, index) => (
                    <div className="full" key={index}>
                      <div className="form-grid">
                        <Field label={t("devices.groups.editor.fact")} full>
                          <select
                            className={inputClass}
                            value={
                              condition.kind === "flat"
                                ? condition.base
                                : condition.kind === "custom"
                                  ? CUSTOM_FACT
                                  : SOFTWARE_FACT
                            }
                            onChange={(event) => {
                              const chosen = event.target.value;
                              if (chosen === CUSTOM_FACT) {
                                setCondition(index, { kind: "custom", base: "" });
                              } else if (chosen === SOFTWARE_FACT) {
                                setCondition(index, { kind: "software", base: "" });
                              } else {
                                setCondition(index, { kind: "flat", base: chosen });
                              }
                            }}
                          >
                            {FLAT_FACTS.map((fact) => (
                              <option key={fact} value={fact}>
                                {t(`devices.groups.fact.${fact}`)}
                              </option>
                            ))}
                            <option value={CUSTOM_FACT}>{t("devices.groups.fact.custom")}</option>
                            <option value={SOFTWARE_FACT}>
                              {t("devices.groups.fact.software")}
                            </option>
                          </select>
                        </Field>
                        {condition.kind !== "flat" ? (
                          <Field
                            label={
                              condition.kind === "custom"
                                ? t("devices.groups.editor.factKey")
                                : t("devices.groups.editor.softwareName")
                            }
                            full
                          >
                            <input
                              className={inputClass}
                              value={condition.base}
                              onChange={(event) =>
                                setCondition(index, { base: event.target.value })
                              }
                            />
                          </Field>
                        ) : null}
                        <Field label={t("devices.groups.editor.comparison")}>
                          <select
                            className={inputClass}
                            value={condition.op}
                            onChange={(event) =>
                              setCondition(index, {
                                op: event.target.value as RuleOp,
                              })
                            }
                          >
                            {OPS.map((op) => (
                              <option key={op} value={op}>
                                {t(`devices.groups.op.${op}`)}
                              </option>
                            ))}
                          </select>
                        </Field>
                        {/* THE CONTROL FOLLOWS THE TYPE, because the evaluator
                            compares with `==` against whatever the agent
                            collected: a boolean fact against the string
                            "true", or a byte count against a string, matches
                            NOTHING while looking exactly right. */}
                        <Field label={t("devices.groups.editor.value")}>
                          {valueControl(condition) === "boolean" ? (
                            <select
                              className={inputClass}
                              value={condition.value === "true" ? "true" : "false"}
                              onChange={(event) =>
                                setCondition(index, { value: event.target.value })
                              }
                            >
                              <option value="true">{t("devices.groups.editor.yes")}</option>
                              <option value="false">{t("devices.groups.editor.no")}</option>
                            </select>
                          ) : (
                            <input
                              className={inputClass}
                              type={
                                valueControl(condition) === "number" ? "number" : "text"
                              }
                              value={condition.value}
                              onChange={(event) =>
                                setCondition(index, { value: event.target.value })
                              }
                            />
                          )}
                        </Field>
                      </div>
                      {editor.conditions.length > 1 ? (
                        <Button
                          variant="ghost"
                          onClick={() =>
                            setEditor({
                              ...editor,
                              conditions: editor.conditions.filter(
                                (_, at) => at !== index,
                              ),
                            })
                          }
                        >
                          {t("devices.groups.editor.removeCondition")}
                        </Button>
                      ) : null}
                    </div>
                  ))}

                  <div className="full">
                    <Button
                      variant="ghost"
                      onClick={() =>
                        setEditor({
                          ...editor,
                          conditions: [...editor.conditions, blankCondition()],
                        })
                      }
                    >
                      {t("devices.groups.editor.addCondition")}
                    </Button>
                  </div>

                  {/* WHAT THIS RULE REACHES, BEFORE IT REACHES IT. Membership
                      is applied by a background sweep, so nothing moves at the
                      moment of saving -- which is exactly why the set has to be
                      on screen at the moment of writing. */}
                  <p className="notice full" role="status">
                    {preview.data
                      ? previewSentence(preview.data, fleetSize)
                      : document === null
                        ? t("devices.groups.preview.finish")
                        : t("devices.groups.preview.checking")}
                  </p>
                  {preview.data && preview.data.sample.length > 0 ? (
                    <div className="full">
                      <p className="note">
                        {preview.data.sample_truncated
                          ? t("devices.groups.preview.firstN", { count: preview.data.sample.length })
                          : t("devices.groups.preview.whereNow")}
                      </p>
                      <ul>
                        {preview.data.sample.map((match) => (
                          <li key={match.device_id}>
                            {match.hostname ?? t("devices.row.unnamed")}:{" "}
                            {match.current_group_name ?? t("devices.groups.preview.orgDefault")}
                          </li>
                        ))}
                      </ul>
                    </div>
                  ) : null}
                </>
              ) : null}
            </div>
            {editor.parentGroupId !== "" ? (
              <Button
                variant="ghost"
                onClick={() => setEditor({ ...editor, parentGroupId: "" })}
              >
                {t("devices.groups.editor.clearParent")}
              </Button>
            ) : null}
            <FormActions
              label={editor.group ? t("devices.groups.editor.save") : t("devices.groups.editor.create")}
              pending={save.isPending}
              error={save.error}
              problems={attemptProblems(attempt)}
              failedMessage={
                editor.group ? t("devices.groups.editor.notSaved") : t("devices.groups.editor.notCreated")
              }
            />
          </Form>
        ) : null}
      </Dialog>

      {/* ----------------------------------------------------------- members */}

      <Dialog
        open={openGroup !== null}
        onClose={() => setOpenGroup(null)}
        title={openGroup ? t("devices.groups.memberDialog.title", { name: openGroup.name }) : t("devices.groups.memberDialog.titleBare")}
      >
        {openGroup ? (
          <>
            {openGroup.dynamic ? (
              <p>
                {t("devices.groups.memberDialog.byRules")}
              </p>
            ) : null}

            {members.length === 0 ? (
              <p className="note">
                {t("devices.groups.memberDialog.none")}
              </p>
            ) : (
              <ul className="mt-3">
                {members.map((device: Device) => (
                  <li key={device.id} className="flex items-center justify-between gap-3">
                    <span>{device.hostname ?? device.description ?? device.id}</span>
                    {canManage && !openGroup.dynamic ? (
                      <Button
                        variant="ghost"
                        onClick={() =>
                          removeMember.mutate({
                            groupId: openGroup.id,
                            deviceId: device.id,
                          })
                        }
                      >
                        {t("devices.groups.memberDialog.remove")}
                      </Button>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}

            {canManage && !openGroup.dynamic ? (
              <div className="mt-3">
                <EntityPicker
                  label={t("devices.groups.memberDialog.add")}
                  options={joinable.map((device) => ({
                    id: device.id,
                    label: device.hostname ?? device.description ?? t("devices.row.unnamed"),
                    sublabel:
                      device.device_group_id === null
                        ? t("devices.groups.memberDialog.orgDefault")
                        : t("devices.groups.memberDialog.otherGroup"),
                  }))}
                  value={addingDevice}
                  onChange={setAddingDevice}
                  emptyHint={t("devices.groups.memberDialog.addEmpty")}
                />
                <Button
                  disabled={addingDevice === "" || addMember.isPending}
                  onClick={() =>
                    addMember.mutate({ groupId: openGroup.id, deviceId: addingDevice })
                  }
                >
                  {t("devices.groups.memberDialog.addToGroup")}
                </Button>
                <p className="note">
                  {t("devices.groups.memberDialog.fallback")}
                </p>
              </div>
            ) : null}

            {addMember.isError || removeMember.isError ? (
              <p role="alert" className="form-error mt-3">
                {describeSubmitError(addMember.error ?? removeMember.error)}
              </p>
            ) : null}
          </>
        ) : null}
      </Dialog>

      {/* ----------------------------------------------------------- archive */}

      <ConfirmDialog
        open={pendingArchive !== null}
        title={t("devices.groups.archiveDialog.title")}
        subject={pendingArchive?.name ?? ""}
        body={t("devices.groups.archiveDialog.body")}
        confirmLabel={t("devices.groups.archiveDialog.confirm")}
        onConfirm={() => {
          if (pendingArchive) archive.mutate(pendingArchive);
        }}
        onCancel={() => setPendingArchive(null)}
      />

      {/* THE REFUSAL AS A TO-DO LIST, not as the object it arrived as. */}
      <Dialog
        open={blocked !== null}
        onClose={() => setBlocked(null)}
        title={blocked ? t("devices.groups.blocked.title", { name: blocked.group.name }) : t("devices.groups.blocked.titleBare")}
      >
        {blocked ? (
          <>
            <p>
              {t("devices.groups.blocked.body")}
            </p>
            <ul className="mt-3">
              {BLOCKERS.filter((blocker) => blocked.references[blocker.key] > 0).map(
                (blocker) => (
                  <li key={blocker.key}>
                    {blocker.to ? (
                      <Link to={blocker.to} onClick={() => setBlocked(null)}>
                        {blockerLabel(blocker.key, blocked.references[blocker.key])}
                      </Link>
                    ) : (
                      blockerLabel(blocker.key, blocked.references[blocker.key])
                    )}
                  </li>
                ),
              )}
            </ul>
          </>
        ) : null}
      </Dialog>
    </>
  );
}
