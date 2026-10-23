/**
 * Fixtures for notifications: three channels, the rules that route the
 * service desk's events through them, the egress allowlist those channels
 * depend on, queue membership drawn from the service desk's people, and a
 * delivery log with a few dead letters in it so the one alarming tile on
 * the page has something to alarm about.
 *
 * DEV-ONLY BY CONSTRUCTION: reached through `fixtures/client.ts` only, which
 * nothing imports outside `FIXTURE_MODE`.
 */

import type { FixtureMap } from "../../fixtures/client";
import { ApiError } from "../../lib/api";
import { ME_ID, USERS } from "../service-desk/fixtures";
import type {
  AllowlistEntryOut,
  ChannelOut,
  DeliveryOut,
  DeliveryStatus,
  QueueMemberOut,
  RuleOut,
  TestSendResult,
} from "./types";

const MINUTE = 60_000;
const DAY = 24 * 60;

const ago = (minutes: number): string => new Date(Date.now() - minutes * MINUTE).toISOString();
const inMinutes = (minutes: number): string => new Date(Date.now() + minutes * MINUTE).toISOString();
const notFound = (what: string) => new ApiError(404, JSON.stringify({ detail: `${what} not found` }));

/* ---------------------------------------------------------------- channels */

const channels: ChannelOut[] = [
  { id: "ch-mattermost", kind: "webhook", name: "Ops Mattermost", config: { url: "https://mattermost.lan:8065/hooks/noc-alerts" }, active: true, has_credential: true, created_at: ago(200 * DAY) },
  { id: "ch-smtp", kind: "email", name: "Helpdesk mail relay", config: { host: "smtp-relay.lan", port: 587, from_address: "helpdesk@toeshee.example", use_tls: true }, active: true, has_credential: true, created_at: ago(200 * DAY) },
  { id: "ch-siem", kind: "webhook", name: "SIEM collector", config: { url: "https://siem.toeshee.example/ingest/itms" }, active: false, has_credential: false, created_at: ago(40 * DAY) },
];

const listNotificationChannels: FixtureMap["listNotificationChannels"] = () => [...channels];

const createNotificationChannel: FixtureMap["createNotificationChannel"] = (body) => {
  const next: ChannelOut = {
    id: `ch-${channels.length + 1}`,
    kind: body.kind,
    name: body.name,
    config: body.config,
    active: body.active ?? true,
    has_credential: Boolean(body.secret),
    created_at: ago(0),
  };
  channels.push(next);
  return next;
};

const updateNotificationChannel: FixtureMap["updateNotificationChannel"] = (id, body) => {
  const at = channels.findIndex((c) => c.id === id);
  const current = channels[at];
  if (at < 0 || !current) throw notFound("channel");
  const next: ChannelOut = {
    ...current,
    ...(body.name != null ? { name: body.name } : {}),
    ...(body.config != null ? { config: body.config } : {}),
    ...(body.active != null ? { active: body.active } : {}),
    has_credential: current.has_credential || Boolean(body.secret),
  };
  channels[at] = next;
  return next;
};

const deleteNotificationChannel: FixtureMap["deleteNotificationChannel"] = (id) => {
  const at = channels.findIndex((c) => c.id === id);
  if (at < 0) throw notFound("channel");
  channels.splice(at, 1);
  return undefined;
};

const testNotificationChannel: FixtureMap["testNotificationChannel"] = (id): TestSendResult => {
  const c = channels.find((channel) => channel.id === id);
  if (!c) throw notFound("channel");
  if (!c.active) return { delivered: false, error: "channel_disabled" };
  return c.kind === "webhook" ? { delivered: true, status_code: 204 } : { delivered: true, status_code: null };
};

/* ------------------------------------------------------------------- rules */

const rules: RuleOut[] = [
  { id: "rule-1", event_type: "ticket.assigned", channel_id: "ch-mattermost", recipient_kind: "assignee", queue_id: null, active: true, created_at: ago(200 * DAY) },
  { id: "rule-2", event_type: "ticket.status_changed", channel_id: "ch-smtp", recipient_kind: "requester", queue_id: null, active: true, created_at: ago(200 * DAY) },
  { id: "rule-3", event_type: "ticket.comment_added", channel_id: "ch-smtp", recipient_kind: "requester", queue_id: null, active: true, created_at: ago(200 * DAY) },
  { id: "rule-4", event_type: "ticket.approval_requested", channel_id: "ch-smtp", recipient_kind: "queue_members", queue_id: "q-hardware", active: true, created_at: ago(120 * DAY) },
  { id: "rule-5", event_type: "ticket.internal_note_added", channel_id: "ch-mattermost", recipient_kind: "queue_members", queue_id: "q-network", active: true, created_at: ago(90 * DAY) },
  { id: "rule-6", event_type: "ticket.resolved", channel_id: "ch-siem", recipient_kind: "channel_default", queue_id: null, active: false, created_at: ago(40 * DAY) },
];

const listNotificationRules: FixtureMap["listNotificationRules"] = () => [...rules];

const createNotificationRule: FixtureMap["createNotificationRule"] = (body) => {
  const next: RuleOut = {
    id: `rule-${rules.length + 1}`,
    event_type: body.event_type,
    channel_id: body.channel_id,
    recipient_kind: body.recipient_kind,
    queue_id: body.queue_id ?? null,
    active: body.active ?? true,
    created_at: ago(0),
  };
  rules.push(next);
  return next;
};

const updateNotificationRule: FixtureMap["updateNotificationRule"] = (id, body) => {
  const at = rules.findIndex((r) => r.id === id);
  const current = rules[at];
  if (at < 0 || !current) throw notFound("rule");
  const next: RuleOut = { ...current, ...(body.active != null ? { active: body.active } : {}) };
  rules[at] = next;
  return next;
};

const deleteNotificationRule: FixtureMap["deleteNotificationRule"] = (id) => {
  const at = rules.findIndex((r) => r.id === id);
  if (at < 0) throw notFound("rule");
  rules.splice(at, 1);
  return undefined;
};

/* --------------------------------------------------------------- allowlist */

const allowlist: AllowlistEntryOut[] = [
  { id: "al-1", host: "mattermost.lan", port: 8065, note: "Self-hosted Mattermost, NOC channel", created_at: ago(200 * DAY) },
  { id: "al-2", host: "smtp-relay.lan", port: 587, note: "Internal mail relay", created_at: ago(200 * DAY) },
];

const listNotificationAllowlist: FixtureMap["listNotificationAllowlist"] = () => [...allowlist];

const createNotificationAllowlistEntry: FixtureMap["createNotificationAllowlistEntry"] = (body) => {
  const next: AllowlistEntryOut = {
    id: `al-${allowlist.length + 1}`,
    host: body.host,
    port: body.port ?? 443,
    note: body.note ?? "",
    created_at: ago(0),
  };
  allowlist.push(next);
  return next;
};

const deleteNotificationAllowlistEntry: FixtureMap["deleteNotificationAllowlistEntry"] = (id) => {
  const at = allowlist.findIndex((e) => e.id === id);
  if (at < 0) throw notFound("allowlist entry");
  allowlist.splice(at, 1);
  return undefined;
};

/* ------------------------------------------------------------ queue members */

const members: QueueMemberOut[] = [
  { queue_id: "q-network", user_id: "usr-mr" },
  { queue_id: "q-network", user_id: ME_ID },
  { queue_id: "q-hardware", user_id: "usr-tk" },
  { queue_id: "q-hardware", user_id: ME_ID },
  { queue_id: "q-facilities", user_id: "usr-tk" },
  { queue_id: "q-access", user_id: "usr-ao" },
];

const listNotificationQueueMembers: FixtureMap["listNotificationQueueMembers"] = (queueId) =>
  members.filter((m) => m.queue_id === queueId);

const addNotificationQueueMember: FixtureMap["addNotificationQueueMember"] = (queueId, userId) => {
  if (!USERS.some((u) => u.id === userId)) throw notFound("user");
  const next: QueueMemberOut = { queue_id: queueId, user_id: userId };
  if (!members.some((m) => m.queue_id === queueId && m.user_id === userId)) members.push(next);
  return next;
};

const removeNotificationQueueMember: FixtureMap["removeNotificationQueueMember"] = (queueId, userId) => {
  const at = members.findIndex((m) => m.queue_id === queueId && m.user_id === userId);
  if (at < 0) throw notFound("queue member");
  members.splice(at, 1);
  return undefined;
};

/* -------------------------------------------------------------- deliveries */

interface DeliverySeed {
  id: string;
  event: string;
  channel: string;
  recipient: string;
  status: DeliveryStatus;
  attempts: number;
  error: string | null;
  code: number | null;
  /** Minutes ago the row was created. */
  at: number;
  next?: number;
}

const DELIVERY_SEEDS: DeliverySeed[] = [
  { id: "dl-1", event: "evt-1", channel: "ch-mattermost", recipient: "mattermost.lan", status: "sent", attempts: 1, error: null, code: 204, at: 60 },
  { id: "dl-2", event: "evt-2", channel: "ch-smtp", recipient: "adaeze.okonkwo@toeshee.example", status: "sent", attempts: 1, error: null, code: null, at: 58 },
  { id: "dl-3", event: "evt-3", channel: "ch-smtp", recipient: "leila.farah@toeshee.example", status: "sent", attempts: 2, error: "smtp 451 greylisted", code: null, at: 5 * 60 },
  { id: "dl-4", event: "evt-4", channel: "ch-mattermost", recipient: "mattermost.lan", status: "failed", attempts: 2, error: "request timed out", code: null, at: 25, next: 20 },
  { id: "dl-5", event: "evt-5", channel: "ch-smtp", recipient: "ines.caetano@toeshee.example", status: "pending", attempts: 0, error: null, code: null, at: 3, next: 1 },
  { id: "dl-6", event: "evt-6", channel: "ch-siem", recipient: "siem.toeshee.example", status: "dead", attempts: 5, error: "egress blocked: blocked_private", code: null, at: 2 * DAY },
  { id: "dl-7", event: "evt-7", channel: "ch-siem", recipient: "siem.toeshee.example", status: "dead", attempts: 5, error: "egress blocked: blocked_private", code: null, at: 2 * DAY - 30 },
  { id: "dl-8", event: "evt-8", channel: "ch-smtp", recipient: "rui.santos@toeshee.example", status: "dead", attempts: 5, error: "smtp 550 rejected", code: null, at: 6 * DAY },
  { id: "dl-9", event: "evt-9", channel: "ch-mattermost", recipient: "mattermost.lan", status: "sent", attempts: 1, error: null, code: 204, at: 3 * 60 },
  { id: "dl-10", event: "evt-10", channel: "ch-smtp", recipient: "tomas.kalu@toeshee.example", status: "sent", attempts: 1, error: null, code: null, at: 26 * 60 },
  { id: "dl-11", event: "evt-11", channel: "ch-mattermost", recipient: "mattermost.lan", status: "sent", attempts: 1, error: null, code: 204, at: DAY + 40 },
  { id: "dl-12", event: "evt-12", channel: "ch-smtp", recipient: "mateus.ribeiro@toeshee.example", status: "sent", attempts: 1, error: null, code: null, at: DAY + 90 },
];

const delivery = (seed: DeliverySeed): DeliveryOut => ({
  id: seed.id,
  event_id: seed.event,
  channel_id: seed.channel,
  recipient: seed.recipient,
  status: seed.status,
  attempts: seed.attempts,
  last_error: seed.error,
  last_status_code: seed.code,
  next_attempt_at: seed.next === undefined ? null : inMinutes(seed.next),
  sent_at: seed.status === "sent" ? ago(seed.at - 1) : null,
  created_at: ago(seed.at),
});

const listNotificationDeliveries: FixtureMap["listNotificationDeliveries"] = (filters = {}) => {
  const matched = DELIVERY_SEEDS.filter(
    (d) =>
      (!filters.status || d.status === filters.status) &&
      (!filters.channel_id || d.channel === filters.channel_id),
  ).sort((a, b) => a.at - b.at);
  const offset = filters.offset ?? 0;
  const limit = filters.limit ?? 50;
  return { items: matched.slice(offset, offset + limit).map(delivery), total: matched.length };
};

const fixtures: FixtureMap = {
  listNotificationChannels,
  createNotificationChannel,
  updateNotificationChannel,
  deleteNotificationChannel,
  testNotificationChannel,
  listNotificationRules,
  createNotificationRule,
  updateNotificationRule,
  deleteNotificationRule,
  listNotificationAllowlist,
  createNotificationAllowlistEntry,
  deleteNotificationAllowlistEntry,
  listNotificationQueueMembers,
  addNotificationQueueMember,
  removeNotificationQueueMember,
  listNotificationDeliveries,
};

export default fixtures;
