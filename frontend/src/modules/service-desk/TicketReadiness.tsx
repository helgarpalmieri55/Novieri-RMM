// "Before tickets are answered on time": what other screens set up that the
// ticket list depends on, and where each is fixed.
//
// THE OPERATOR ASKED FOR IT (2026-09-24, `docs/design/cross-module-guidance.md`
// §2 Tickets, §6 #9). Every row is advisory -- tickets work without any of
// them -- but each one missing is a silent gap:
//
//   1  response and resolution targets exist (an active SLA policy); without
//      one every ticket reads "No SLA" and nothing is ever late;
//   2  every category has a default queue; a ticket filed in a category
//      without one is refused unless the filer names a queue
//      (`service_desk/tickets.py`);
//   3  a support mailbox turns mail into tickets;
//   4  alerts can open tickets (a default alert queue and category);
//   5  someone is told about ticket activity.
//
// Skipped from the doc's table: "requesters exist" (portal accounts). The
// requester role's assignment count says nothing about mail senders, so a
// zero there does not mean nobody can raise a ticket.
//
// NOTHING HERE GATES ANYTHING (invariant 6).

import { useQuery } from "@tanstack/react-query";

import { prerequisiteFromQuery, Prerequisites } from "../../components/prerequisites";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import type { ApiError } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { joinWords, mayCheck, useRulesPrerequisite } from "../../lib/detectors";
import { useMePermissions } from "../../lib/mePermissions";
import type { MailboxOut } from "../email-intake/types";
import type { SettingsOut as HealthSettingsOut } from "../health/types";
import { settingsHref } from "../settings/tabs";
import type { PolicyOut } from "../sla/types";
import type { CategoryOut } from "./types";

export const SLA_READ = "sla.read";
export const TICKET_READ = "ticket.read";
export const EMAIL_INTAKE_READ = "email_intake.read";
export const HEALTH_READ = "health.read";

// The label is an accessor over the catalogue, so it follows a language switch.
export const SET_TARGETS_CTA = {
  to: "/sla",
  get label(): string {
    return translate("serviceDesk.readiness.setTargetsCta");
  },
};

export function TicketReadiness() {
  const t = useT();
  const api = useApi();
  const me = useMePermissions();

  // Keys and shapes the owning pages already use, so their edits clear a row.
  const policies = useQuery<PolicyOut[], ApiError>({
    queryKey: ["sla-policies"],
    queryFn: () => api.listSlaPolicies(),
    enabled: mayCheck(me, SLA_READ),
    retry: false,
  });
  const categories = useQuery<CategoryOut[], ApiError>({
    queryKey: ["service-desk-categories", false],
    queryFn: () => api.listTicketCategories(false),
    enabled: mayCheck(me, TICKET_READ),
    retry: false,
  });
  const mailboxes = useQuery<MailboxOut[], ApiError>({
    queryKey: ["email-intake-mailboxes"],
    queryFn: () => api.listEmailMailboxes(),
    enabled: mayCheck(me, EMAIL_INTAKE_READ),
    retry: false,
  });
  const healthSettings = useQuery<HealthSettingsOut, ApiError>({
    queryKey: ["health-settings"],
    queryFn: () => api.getHealthSettings(),
    enabled: mayCheck(me, HEALTH_READ),
    retry: false,
  });

  const slaItem = prerequisiteFromQuery<PolicyOut[]>({
    label: t("serviceDesk.readiness.sla.label"),
    query: policies,
    permission: { code: SLA_READ, me },
    isDone: (list) => list.some((policy) => policy.active),
    done: (list) => t("serviceDesk.readiness.sla.done", { count: list.filter((p) => p.active).length }),
    missing: {
      severity: "warn",
      detail: t("serviceDesk.readiness.sla.missing"),
      cta: SET_TARGETS_CTA,
    },
  });

  const queueItem = prerequisiteFromQuery<CategoryOut[]>({
    label: t("serviceDesk.readiness.queue.label"),
    query: categories,
    permission: { code: TICKET_READ, me },
    isDone: (list) => list.every((category) => category.default_queue_id != null),
    done: t("serviceDesk.readiness.queue.done"),
    missing: (list) => {
      const names = list.filter((category) => category.default_queue_id == null).map((category) => category.name);
      return {
        severity: "warn",
        detail: t("serviceDesk.readiness.queue.missing", { count: names.length, names: joinWords(names) }),
        cta: { to: "/service-desk/admin", label: t("serviceDesk.readiness.queue.cta") },
      };
    },
  });

  const mailboxItem = prerequisiteFromQuery<MailboxOut[]>({
    label: t("serviceDesk.readiness.mailbox.label"),
    query: mailboxes,
    permission: { code: EMAIL_INTAKE_READ, me },
    isDone: (list) => list.length > 0,
    done: (list) => t("serviceDesk.readiness.mailbox.done", { count: list.length }),
    missing: {
      severity: "warn",
      detail: t("serviceDesk.readiness.mailbox.missing"),
      cta: { to: "/email-intake#mailboxes", label: t("serviceDesk.readiness.mailbox.cta") },
    },
  });

  const alertItem = prerequisiteFromQuery<HealthSettingsOut>({
    label: t("serviceDesk.readiness.alert.label"),
    query: healthSettings,
    permission: { code: HEALTH_READ, me },
    isDone: (settings) => settings.alert_tickets_enabled,
    done: t("serviceDesk.readiness.alert.done"),
    missing: {
      severity: "warn",
      detail: t("serviceDesk.readiness.alert.missing"),
      cta: { to: settingsHref("health"), label: t("serviceDesk.readiness.alert.cta") },
    },
  });

  const rulesItem = useRulesPrerequisite("ticket.", {
    label: t("serviceDesk.readiness.rules.label"),
    missingDetail: t("serviceDesk.readiness.rules.missing"),
    ctaLabel: t("serviceDesk.readiness.rules.cta"),
  });

  return (
    <Prerequisites
      title={t("serviceDesk.readiness.title")}
      subtitle={t("serviceDesk.readiness.subtitle")}
      items={[slaItem, queueItem, mailboxItem, alertItem, rulesItem]}
    />
  );
}
