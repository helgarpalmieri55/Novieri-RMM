// Notifications — channels and rules, edited here rather than linked to.
//
//   read   `GET /api/v1/notifications/channels|rules`     `notification.read`
//   write  `POST|PATCH /api/v1/notifications/channels`    `notification.admin`
//          `POST /api/v1/notifications/channels/{id}/test`
//          `POST|PATCH /api/v1/notifications/rules`
//
// SECRETS ARE WRITE-ONLY, AND THE API MAKES THAT EASY TO HOLD (invariant 3).
// `ChannelOut` carries no credential field at all — not the secret, not even a
// `credential_id` — only `has_credential`. So there is nothing to pre-fill and
// nothing to leak: the secret box is always empty, is `type="password"`, and
// "leave blank to keep the current one" is a statement about what the server
// does with an omitted `secret`, not a UI convention. The rendered state of a
// credential on this page is the word "stored"/"none", forever.
//
// A rule's event type, channel, recipient kind and queue are IMMUTABLE after
// creation (`RuleUpdate` carries `active` only) — they together define what
// the rule routes. So the rules table toggles `active` and nothing else, and a
// different routing means a new rule. Offering editable boxes that the API
// would ignore is precisely the "form that discards what you typed" this page
// was rebuilt to eliminate.

import { useState, type CSSProperties } from "react";
import { Link } from "react-router-dom";

import { describeSubmitError } from "../../components/form";
import { EntityPicker } from "../../components/picker";
import type { PickerOption } from "../../components/picker";
import { Badge, Button, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { lazyFields } from "../../i18n/lazy";
import { Trans } from "../../i18n/Trans";
import { humaniseStatus } from "../../lib/labels";
import { Btn, FieldGroup, KIND_ICON, Panel, QueryState } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { ChipsField } from "../../shell/viz";
import {
  EMPTY_SIGN_IN,
  EmailSignInFields,
  MICROSOFT_SMTP,
  signInProblem,
  signInRequest,
} from "../notification/EmailSignInFields";
import type { EmailSignIn } from "../notification/EmailSignInFields";
import { eventTypeLabel, recipientKindLabel } from "../notification/labels";
import { deliveryErrorMessage } from "../notification/notificationState";
import { EVENT_TYPES } from "../notification/types";
import type { ChannelKind, ChannelOut, RecipientKind } from "../notification/types";
import {
  useCreateChannelMutation,
  useCreateRuleMutation,
  useNotificationChannels,
  useNotificationRules,
  useTestChannelMutation,
  useTicketQueues,
  useUpdateChannelMutation,
  useUpdateRuleMutation,
  useGlobalWriteReason,
} from "./api";
import { isForbidden } from "./settingsState";
import { FOLDING_FORM_GRID } from "./formGrid";

export const NOTIFICATION_READ = "notification.read";
export const NOTIFICATION_ADMIN = "notification.admin";

const RECIPIENT_KINDS: readonly RecipientKind[] = [
  "queue_members",
  "assignee",
  "requester",
  "channel_default",
];

/**
 * A channel's kind: two fixed, server-defined literals (`ChannelKind`), not a
 * server-fed list — the same shape `RECIPIENT_KINDS` above already is, and
 * the reason both are chips now rather than one being a chip group and the
 * other a `<select>` with no stated reason (`docs/superpowers/specs/
 * 2026-09-10-settings-ui-rework-design.md` §4: "small, fixed and nameable").
 * Title Case here, not the raw wire token `webhook`/`email` the old
 * `<option>` printed -- the same plain-language-over-the-wire-value move
 * `eventTypeLabel`/`recipientKindLabel` already make for Routing's two
 * fields, just inline: two entries did not earn a `labels.ts` export.
 */
const CHANNEL_KIND_CHOICES: readonly { value: ChannelKind; label: string }[] = [
  lazyFields({ value: "webhook" as ChannelKind }, { label: "enums.ChannelKind.webhook" }),
  lazyFields({ value: "email" as ChannelKind }, { label: "enums.ChannelKind.email" }),
];

/**
 * The same Title Case in the table as in the chips above it.
 *
 * Review finding, casing drift: the KIND column printed `channel.kind`
 * straight off the wire (`webhook`/`email`, lowercase) two inches below the
 * chips that spell the same two values `Webhook`/`Email` for the create
 * form — one tab, two casings for one enum. `CHANNEL_KIND_CHOICES` is
 * already the one place that maps the wire token to words; this reads it
 * rather than keeping a second map that could drift from it.
 */
function channelKindLabel(kind: ChannelKind): string {
  return CHANNEL_KIND_CHOICES.find((choice) => choice.value === kind)?.label ?? humaniseStatus(kind);
}

// `EVENT_TYPES[0]` is `string | undefined` under `noUncheckedIndexedAccess`
// even though the array is a non-empty literal; a named constant sidesteps
// that without a non-null assertion.
const DEFAULT_EVENT_TYPE = "ticket.assigned";

/** Lines under a bare panel's table sit on the panel's own padding. */
const UNDER_TABLE: CSSProperties = { padding: "12px 18px" };

interface ChannelEdit {
  name: string;
  active: boolean;
  /** Always starts empty and is cleared after every save. Never seeded from the server. */
  secret: string;
}

export function NotificationsTab({
  canRead,
  canAdmin,
}: {
  canRead: boolean;
  canAdmin: boolean;
}) {
  const t = useT();
  const reason = useGlobalWriteReason(NOTIFICATION_ADMIN);
  const channels = useNotificationChannels(canRead);
  const rules = useNotificationRules(canRead);
  const queues = useTicketQueues(canRead);
  const createChannel = useCreateChannelMutation();
  const updateChannel = useUpdateChannelMutation();
  const testChannel = useTestChannelMutation();
  const createRule = useCreateRuleMutation();
  const updateRule = useUpdateRuleMutation();

  const [edits, setEdits] = useState<Record<string, ChannelEdit>>({});
  const [targetChannelId, setTargetChannelId] = useState<string | null>(null);
  const [testedChannelId, setTestedChannelId] = useState<string | null>(null);

  const [newKind, setNewKind] = useState<ChannelKind>("webhook");
  const [newName, setNewName] = useState("");
  const [newUrl, setNewUrl] = useState("");
  const [newHost, setNewHost] = useState("");
  const [newPort, setNewPort] = useState("587");
  const [newFrom, setNewFrom] = useState("");
  const [newUseTls, setNewUseTls] = useState(true);
  const [newSecret, setNewSecret] = useState("");
  const [newSignIn, setNewSignIn] = useState<EmailSignIn>(EMPTY_SIGN_IN);

  const [eventType, setEventType] = useState<string>(DEFAULT_EVENT_TYPE);
  const [ruleChannelId, setRuleChannelId] = useState("");
  const [recipientKind, setRecipientKind] = useState<RecipientKind>("queue_members");
  const [queueId, setQueueId] = useState("");
  const [targetRuleId, setTargetRuleId] = useState<string | null>(null);

  const forbidden = isForbidden(channels.error);
  const failed = channels.error != null && !forbidden;
  // `&& queues.data === undefined` (review finding 3, matching
  // `HealthTab.tsx`'s identical fix): a failed background REFETCH must not
  // tear down a picker that already has a good cached list to show, over one
  // transient failure.
  const queuesUnavailable = queues.error != null && queues.data === undefined;

  // "Any queue" is a value (`queue_id: null`), not the absence of a choice
  // -- explicit here rather than left as what an empty search happens to mean.
  // Built at render, not remembered: the first option is a worded label, and a
  // memo would keep it in the language it was built in.
  const queueOptions: PickerOption[] = [
    { id: "", label: t("settings.notifications.rules.anyQueue") },
    ...(queues.data ?? []).map((queue) => ({ id: queue.id, label: queue.name, sublabel: queue.key })),
  ];

  function editFor(channel: ChannelOut): ChannelEdit {
    return edits[channel.id] ?? { name: channel.name, active: channel.active, secret: "" };
  }

  function setEdit(channel: ChannelOut, patch: Partial<ChannelEdit>) {
    setEdits((current) => ({ ...current, [channel.id]: { ...editFor(channel), ...patch } }));
  }

  function channelDirty(channel: ChannelOut): boolean {
    const edit = edits[channel.id];
    if (!edit) return false;
    return edit.name !== channel.name || edit.active !== channel.active || edit.secret !== "";
  }

  function saveChannel(channel: ChannelOut) {
    const edit = editFor(channel);
    setTargetChannelId(channel.id);
    updateChannel.mutate(
      {
        id: channel.id,
        body: {
          name: edit.name,
          active: edit.active,
          // Omitted, not sent empty: an empty string is a credential of zero
          // length, `undefined` is "leave the stored one alone".
          secret: edit.secret === "" ? undefined : edit.secret,
        },
      },
      {
        onSuccess: () => {
          // Drop the local edit so the row re-seeds from the server's answer,
          // and the secret box empties.
          setEdits((current) => {
            const next = { ...current };
            delete next[channel.id];
            return next;
          });
        },
      },
    );
  }

  function submitNewChannel() {
    // Spec §2.2: a webhook channel's config is `{url}` only — the backend
    // builds its own headers, and a caller-supplied `headers` dict would be a
    // place for a secret to land in a response.
    const microsoft = newSignIn.auth === "microsoft";
    const config =
      newKind === "webhook"
        ? { url: newUrl }
        : microsoft
          ? {
              auth: "microsoft",
              host: MICROSOFT_SMTP.host,
              port: MICROSOFT_SMTP.port,
              from_address: newFrom,
              use_tls: true,
            }
          : {
              auth: "password",
              host: newHost,
              port: Number(newPort),
              from_address: newFrom,
              use_tls: newUseTls,
            };
    createChannel.mutate(
      newKind === "webhook"
        ? { kind: newKind, name: newName, config, secret: newSecret === "" ? undefined : newSecret }
        : { kind: newKind, name: newName, config, ...signInRequest(newSignIn) },
      {
        onSuccess: () => {
          setNewName("");
          setNewUrl("");
          setNewHost("");
          setNewFrom("");
          setNewSecret("");
          setNewSignIn(EMPTY_SIGN_IN);
        },
      },
    );
  }

  function submitNewRule() {
    createRule.mutate(
      {
        event_type: eventType,
        channel_id: ruleChannelId,
        recipient_kind: recipientKind,
        queue_id: queueId === "" ? null : queueId,
      },
      {
        onSuccess: () => {
          setQueueId("");
        },
      },
    );
  }

  // §6.4: an internal note cannot be routed to the requester. Rejected at
  // write time server-side; hidden here rather than offered and refused.
  const recipientOptions =
    eventType === "ticket.internal_note_added"
      ? RECIPIENT_KINDS.filter((kind) => kind !== "requester")
      : RECIPIENT_KINDS;

  const newSignInProblem = newKind === "email" ? signInProblem(newSignIn, false) : null;
  const newChannelValid =
    newName !== "" &&
    (newKind === "webhook"
      ? newUrl !== ""
      : (newSignIn.auth === "microsoft" || newHost !== "") && newFrom !== "" && !newSignInProblem);

  return (
    <>
      <Panel
        title={t("settings.notifications.channels.title")}
        subtitle={t("settings.notifications.channels.subtitle")}
        bare
      >
        {forbidden ? (
          <p className="note" style={UNDER_TABLE}>
            {t("settings.notifications.channels.forbidden", { code: NOTIFICATION_READ })}
          </p>
        ) : (
          <>
            {!canAdmin ? (
              <p className="note" style={UNDER_TABLE}>{reason}</p>
            ) : null}
            {/* The design's three states. "No channel is configured" and "the
                notification service is down" mean very different things to
                someone about to rely on an alert reaching them. */}
            <QueryState
              loading={channels.isLoading}
              error={failed ? channels.error : null}
              empty={(channels.data ?? []).length === 0}
              emptyIcon="bell"
              emptyLabel={t("settings.notifications.channels.empty")}
            >
              <Table>
                <thead>
                  <tr>
                    {/* Review finding: at 390px this cell's `<input>` measured
                        24px wide against 125-147px of channel names, so three
                        channels rendered as three empty boxes. The table is
                        auto-layout (`Th`'s own doc), so this is a floor the
                        column may exceed, never a cap -- the same fix
                        `AddressGrid.tsx`'s `Hostname` column already uses for
                        an identical text input in a narrow cell. 200px is
                        measured, not guessed: a canvas `measureText` at the
                        panel's own 13.5px font against the longest fixture
                        name ("Helpdesk mail relay") clears it with margin, at
                        both 390 and 1440 (verified with Playwright against
                        `?__fixtures` -- jsdom has no layout to check this
                        against). */}
                    <Th width={200}>{t("settings.notifications.channels.columns.name")}</Th>
                    <Th>{t("settings.notifications.channels.columns.kind")}</Th>
                    <Th>{t("settings.notifications.channels.columns.credential")}</Th>
                    <Th>{t("settings.notifications.channels.columns.active")}</Th>
                    {/* At 1440px the box was 183px and "Leave blank to keep
                        the current one" (the placeholder
                        `docs/design/settings-language.md`'s Notifications
                        rewrite section names verbatim) rendered truncated as
                        "Leave blank to keep the c". The box earns its width
                        instead of the placeholder losing its words -- 280px,
                        the same measured-not-guessed floor as Name above,
                        chosen so the table's total width lands exactly at the
                        panel's own width at 1440 (no new horizontal scroll
                        introduced there) while still clearing 390 with the
                        panel's built-in "scroll for more columns" hint doing
                        the rest. */}
                    <Th width={280}>{t("settings.notifications.channels.columns.newToken")}</Th>
                    <Th>{""}</Th>
                  </tr>
                </thead>
                <tbody>
                  {(channels.data ?? []).map((channel) => {
                    const edit = editFor(channel);
                    return (
                      <tr key={channel.id}>
                        <Td lead={<Icon name={channel.kind === "email" ? "mail" : "link"} size={18} />}>
                          <input
                            aria-label={t("settings.notifications.channels.nameAria", { name: channel.name })}
                            className={inputClass}
                            value={edit.name}
                            disabled={!canAdmin}
                            onChange={(event) => setEdit(channel, { name: event.target.value })}
                          />
                        </Td>
                        <Td>{channelKindLabel(channel.kind)}</Td>
                        <Td>
                          {/* Invariant 3: existence only, never the credential. */}
                          <Badge variant={channel.has_credential ? "success" : "muted"}>
                            {channel.has_credential ? t("settings.notifications.channels.storedInVault") : t("settings.notifications.channels.noCredential")}
                          </Badge>
                        </Td>
                        <Td>
                          <label className="flex items-center gap-2">
                            <input
                              type="checkbox"
                              aria-label={t("settings.notifications.channels.activeAria", { name: channel.name })}
                              checked={edit.active}
                              disabled={!canAdmin}
                              onChange={(event) =>
                                setEdit(channel, { active: event.target.checked })
                              }
                            />
                            {edit.active ? t("settings.notifications.channels.active") : t("settings.notifications.channels.inactive")}
                          </label>
                        </Td>
                        <Td>
                          {channel.kind === "webhook" ? (
                            <input
                              type="password"
                              aria-label={t("settings.notifications.channels.tokenAria", { name: channel.name })}
                              placeholder={t("settings.notifications.channels.tokenPlaceholder")}
                              className={inputClass}
                              value={edit.secret}
                              disabled={!canAdmin}
                              onChange={(event) => setEdit(channel, { secret: event.target.value })}
                            />
                          ) : (
                            // A mail sign-in is more than one value; it is
                            // edited where all of them are.
                            <span className="form-hint">{t("settings.notifications.channels.mailHint")}</span>
                          )}
                        </Td>
                        <Td>
                          <div className="flex flex-wrap items-center gap-2">
                            <Button
                              disabled={
                                !canAdmin || !channelDirty(channel) || updateChannel.isPending
                              }
                              onClick={() => saveChannel(channel)}
                            >
                              {t("common.actions.save")}
                            </Button>
                            <Button
                              variant="outline"
                              disabled={!canAdmin || testChannel.isPending}
                              onClick={() => {
                                setTestedChannelId(channel.id);
                                testChannel.mutate(channel.id);
                              }}
                            >
                              {t("settings.notifications.channels.sendTest")}
                            </Button>
                          </div>
                          {targetChannelId === channel.id && updateChannel.isError ? (
                            <p role="alert" className="form-error" style={{ marginTop: 4 }}>
                              <Icon name={KIND_ICON.crit} size={13} />{" "}
                              {describeSubmitError(updateChannel.error)}
                            </p>
                          ) : null}
                          {targetChannelId === channel.id &&
                          updateChannel.isSuccess &&
                          !channelDirty(channel) ? (
                            <p role="status" className="form-saved" style={{ marginTop: 4 }}>
                              <Icon name={KIND_ICON.ok} size={13} /> {t("settings.notifications.channels.saved")}
                            </p>
                          ) : null}
                          {testedChannelId === channel.id && testChannel.isSuccess ? (
                            <p
                              role="status"
                              className={testChannel.data.delivered ? "form-saved" : "form-error"}
                              style={{ marginTop: 4 }}
                            >
                              <Icon name={testChannel.data.delivered ? KIND_ICON.ok : KIND_ICON.crit} size={13} />{" "}
                              {testChannel.data.delivered
                                ? t("settings.notifications.channels.testDelivered", {
                                    code: testChannel.data.status_code ?? "ok",
                                  })
                                : t("settings.notifications.channels.testNotDelivered", {
                                    reason: deliveryErrorMessage(testChannel.data.error),
                                  })}
                            </p>
                          ) : null}
                          {testedChannelId === channel.id && testChannel.isError ? (
                            <p role="alert" className="form-error" style={{ marginTop: 4 }}>
                              <Icon name={KIND_ICON.crit} size={13} />{" "}
                              {describeSubmitError(testChannel.error)}
                            </p>
                          ) : null}
                        </Td>
                      </tr>
                    );
                  })}
                </tbody>
              </Table>
            </QueryState>
          </>
        )}
      </Panel>

      {canRead ? (
        <Panel title={t("settings.notifications.add.title")} subtitle={t("settings.notifications.add.subtitle")}>
          <FieldGroup title={t("settings.notifications.add.group")} icon="bell">
            <div className="form-grid" style={FOLDING_FORM_GRID}>
              {/* A CHOICE OF TWO, NOT A MENU — the same criterion Email's
                  Encryption and Budget's fiscal year already read as chips,
                  applied here and to Recipients below so the page does not
                  draw one small fixed enumeration as a chip group and
                  another, right beside it, as a `<select>` with no stated
                  reason. */}
              <ChipsField
                caption={t("settings.notifications.add.kind")}
                items={CHANNEL_KIND_CHOICES.map((choice) => choice.label)}
                active={CHANNEL_KIND_CHOICES.findIndex((choice) => choice.value === newKind)}
                disabled={!canAdmin}
                onSelect={(at) => {
                  const choice = CHANNEL_KIND_CHOICES[at];
                  if (choice) setNewKind(choice.value);
                }}
              />
              <Field label={t("settings.notifications.add.name")}>
                <input
                  className={inputClass}
                  value={newName}
                  disabled={!canAdmin}
                  onChange={(event) => setNewName(event.target.value)}
                />
              </Field>
              {newKind === "webhook" ? (
                <>
                  <Field label={t("settings.notifications.add.url")}>
                    <input
                      className={inputClass}
                      value={newUrl}
                      disabled={!canAdmin}
                      onChange={(event) => setNewUrl(event.target.value)}
                    />
                  </Field>
                  <Field label={t("settings.notifications.add.bearer")} help={t("settings.notifications.add.optional")}>
                    <input
                      className={inputClass}
                      type="password"
                      value={newSecret}
                      disabled={!canAdmin}
                      onChange={(event) => setNewSecret(event.target.value)}
                    />
                  </Field>
                </>
              ) : (
                <>
                  {newSignIn.auth === "password" ? (
                    <>
                      <Field label={t("settings.notifications.add.smtpHost")}>
                        <input
                          className={inputClass}
                          value={newHost}
                          disabled={!canAdmin}
                          onChange={(event) => setNewHost(event.target.value)}
                        />
                      </Field>
                      <Field label={t("settings.notifications.add.smtpPort")}>
                        <input
                          className={inputClass}
                          type="number"
                          value={newPort}
                          disabled={!canAdmin}
                          onChange={(event) => setNewPort(event.target.value)}
                        />
                      </Field>
                    </>
                  ) : null}
                  <Field label={t("settings.notifications.add.fromAddress")}>
                    <input
                      className={inputClass}
                      value={newFrom}
                      disabled={!canAdmin}
                      onChange={(event) => setNewFrom(event.target.value)}
                    />
                  </Field>
                  <EmailSignInFields
                    value={newSignIn}
                    onChange={setNewSignIn}
                    stored={false}
                    disabled={!canAdmin}
                  />
                  {newSignIn.auth === "password" ? (
                    <label className="flex items-center gap-2 full">
                      <input
                        type="checkbox"
                        checked={newUseTls}
                        disabled={!canAdmin}
                        onChange={(event) => setNewUseTls(event.target.checked)}
                      />
                      {t("settings.notifications.add.useTls")}
                    </label>
                  ) : null}
                  {newSignInProblem && (newSignIn.username || newSignIn.password || newSignIn.tenantId || newSignIn.clientId || newSignIn.clientSecret) ? (
                    <p className="form-hint full">{newSignInProblem}</p>
                  ) : null}
                </>
              )}
            </div>
          </FieldGroup>
          <div className="form-actions">
            <Btn
              label={createChannel.isPending ? t("settings.notifications.add.creating") : t("settings.notifications.add.create")}
              icon="plus"
              variant="primary"
              disabled={!canAdmin || !newChannelValid || createChannel.isPending}
              onClick={submitNewChannel}
            />
            {!canAdmin ? (
              <span className="form-hint">{reason}</span>
            ) : null}
            {createChannel.isError ? (
              <span role="alert" className="form-error">
                <Icon name={KIND_ICON.crit} size={13} />
                {describeSubmitError(createChannel.error)}
              </span>
            ) : null}
            {createChannel.isSuccess ? (
              <span role="status" className="form-saved">
                <Icon name={KIND_ICON.ok} size={13} />
                {t("settings.notifications.add.created")}
              </span>
            ) : null}
          </div>
        </Panel>
      ) : null}

      {canRead ? (
        <Panel
          title={t("settings.notifications.rules.title")}
          subtitle={t("settings.notifications.rules.subtitle")}
          bare
        >
          <QueryState
            loading={rules.isLoading}
            error={rules.error != null && !isForbidden(rules.error) ? rules.error : null}
            empty={(rules.data ?? []).length === 0}
            emptyIcon="bell"
            emptyLabel={t("settings.notifications.rules.empty")}
          >
            <Table>
              <thead>
                <tr>
                  <Th>{t("settings.notifications.rules.columns.event")}</Th>
                  <Th>{t("settings.notifications.rules.columns.channel")}</Th>
                  <Th>{t("settings.notifications.rules.columns.recipients")}</Th>
                  <Th>{t("settings.notifications.rules.columns.queue")}</Th>
                  <Th>{t("settings.notifications.rules.columns.active")}</Th>
                </tr>
              </thead>
              <tbody>
                {(rules.data ?? []).map((rule) => (
                  <tr key={rule.id}>
                    {/* The event type's DISPLAY NAME is the cell's OWN text:
                        the tab's test locates the rule row by the `td` that
                        carries it. The wire value (`rule.event_type`) never
                        reaches the screen -- see notification/labels.ts. */}
                    <Td>{eventTypeLabel(rule.event_type)}</Td>
                    <Td>
                      {channels.data?.find((channel) => channel.id === rule.channel_id)?.name ??
                        rule.channel_id}
                    </Td>
                    <Td>{recipientKindLabel(rule.recipient_kind)}</Td>
                    <Td>
                      {rule.queue_id == null
                        ? t("settings.notifications.rules.anyQueue")
                        : (queues.data?.find((queue) => queue.id === rule.queue_id)?.name ??
                          rule.queue_id)}
                    </Td>
                    <Td>
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge variant={rule.active ? "success" : "muted"}>
                          {rule.active ? t("settings.notifications.rules.active") : t("settings.notifications.rules.inactive")}
                        </Badge>
                        <Button
                          variant="outline"
                          disabled={!canAdmin || updateRule.isPending}
                          onClick={() => {
                            setTargetRuleId(rule.id);
                            updateRule.mutate({ id: rule.id, body: { active: !rule.active } });
                          }}
                        >
                          {rule.active ? t("settings.notifications.rules.disable") : t("settings.notifications.rules.enable")}
                        </Button>
                      </div>
                      {targetRuleId === rule.id && updateRule.isError ? (
                        <p role="alert" className="form-error" style={{ marginTop: 4 }}>
                          <Icon name={KIND_ICON.crit} size={13} />{" "}
                          {describeSubmitError(updateRule.error)}
                        </p>
                      ) : null}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </QueryState>

          <div style={{ padding: "16px 18px", borderTop: "1px solid var(--line)" }}>
            <FieldGroup title={t("settings.notifications.rules.group")} icon="git-branch">
              <div className="form-grid" style={FOLDING_FORM_GRID}>
                <Field label={t("settings.notifications.rules.eventType")}>
                  <select
                    className="select-field"
                    value={eventType}
                    disabled={!canAdmin}
                    onChange={(event) => setEventType(event.target.value)}
                  >
                    {EVENT_TYPES.map((type) => (
                      <option key={type} value={type}>
                        {eventTypeLabel(type)}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label={t("settings.notifications.rules.channel")}>
                  <select
                    className="select-field"
                    value={ruleChannelId}
                    disabled={!canAdmin}
                    onChange={(event) => setRuleChannelId(event.target.value)}
                  >
                    <option value="">{t("settings.notifications.rules.selectChannel")}</option>
                    {(channels.data ?? []).map((channel) => (
                      <option key={channel.id} value={channel.id}>
                        {channel.name}
                      </option>
                    ))}
                  </select>
                </Field>
                {/* A CHOICE OF FOUR (THREE FOR AN INTERNAL NOTE), NOT A
                    MENU — see `Kind` above. `recipientOptions` is still the
                    filtered list §6.4 needs; `ChipsField` only draws it. */}
                <ChipsField
                  caption={t("settings.notifications.rules.recipients")}
                  items={recipientOptions.map((kind) => recipientKindLabel(kind))}
                  active={recipientOptions.indexOf(recipientKind)}
                  disabled={!canAdmin}
                  onSelect={(at) => {
                    const kind = recipientOptions[at];
                    if (kind) setRecipientKind(kind);
                  }}
                />
                {queuesUnavailable ? (
                  // Queues are `ticket.read`, which a notification admin need not
                  // hold. Degrade to the id rather than block rule creation on a
                  // permission this tab does not require. `label` + `help`,
                  // not a parenthetical folded into the label -- the same
                  // shape as `HealthTab.tsx`'s two structurally identical
                  // degrade branches (review finding 7).
                  <Field label={t("settings.notifications.rules.queueId")} help={t("settings.notifications.rules.queueIdHelp")}>
                    <input
                      className={inputClass}
                      value={queueId}
                      disabled={!canAdmin}
                      onChange={(event) => setQueueId(event.target.value)}
                    />
                  </Field>
                ) : (
                  // ONE GRID CELL, NOT TWO. The picker and its hint used to
                  // be separate direct children of `.form-grid`, which is
                  // what broke this group's 2×2: `Recipients` filled row 2's
                  // first column and the picker, a spanning direct child on
                  // its own, could not fit beside it -- it dropped to a new
                  // row and left row 2's second cell empty (review finding
                  // 2). Wrapped together, the group is one ordinary cell and
                  // sits beside `Recipients` the way `git show 7ecccea`'s
                  // plain `<select>` once did.
                  <div className="picker-group">
                    <EntityPicker
                      label={t("settings.notifications.rules.queueScope")}
                      options={queueOptions}
                      loading={queues.isLoading}
                      placeholder={t("settings.notifications.rules.searchQueues")}
                      value={queueId}
                      disabled={!canAdmin}
                      onChange={setQueueId}
                    />
                    {/* Fetched-and-empty is the one state in which "no
                        queues exist yet" is a claim this tab actually knows
                        to be true. (`emptyHint` on the picker above is
                        unreachable here -- the synthetic "Any queue" entry
                        means its own `options.length` is never 0 -- so it is
                        no longer passed at all: review finding 8.) */}
                    {!queues.isLoading && (queues.data ?? []).length === 0 ? (
                      <p className="form-hint">
                        <Trans
                          k="settings.queuePicker.none"
                          components={{ link: <Link to="/service-desk/admin" /> }}
                        />
                      </p>
                    ) : null}
                  </div>
                )}
              </div>
            </FieldGroup>
            <div className="form-actions">
              <Btn
                label={createRule.isPending ? t("settings.notifications.rules.creating") : t("settings.notifications.rules.create")}
                icon="plus"
                variant="primary"
                disabled={!canAdmin || ruleChannelId === "" || createRule.isPending}
                onClick={submitNewRule}
              />
              {!canAdmin ? (
                <span className="form-hint">{reason}</span>
              ) : null}
              {createRule.isError ? (
                <span role="alert" className="form-error">
                  <Icon name={KIND_ICON.crit} size={13} />
                  {describeSubmitError(createRule.error)}
                </span>
              ) : null}
              {createRule.isSuccess ? (
                <span role="status" className="form-saved">
                  <Icon name={KIND_ICON.ok} size={13} />
                  {t("settings.notifications.rules.created")}
                </span>
              ) : null}
            </div>
          </div>
        </Panel>
      ) : null}
    </>
  );
}
