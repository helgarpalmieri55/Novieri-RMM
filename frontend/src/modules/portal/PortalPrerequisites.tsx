// "Before requesters can use the portal": what other modules have to hold for
// the end-user portal to be of use to anyone
// (`docs/design/cross-module-guidance.md` §2, End-user portal).
//
//   1  requester accounts exist -- the portal is for people holding the
//      global `requester` role, and with nobody holding it nobody can sign in
//      to raise a ticket here (blocking);
//   2  help articles are published to the portal (IT documentation);
//   3  the assistant is switched on for this server -- a property of the
//      server's configuration, so this row has no link: nothing in the
//      console turns it on;
//   4  a notification rule tells requesters about their tickets.
//
// SHOWN TO OPERATORS ONLY. A requester lands on this same page, and "requester
// accounts exist" is not their question -- so an account holding nothing but
// portal codes never sees the panel (`isPortalOnly`), and nothing is fetched
// for it.
//
// NOTHING HERE GATES ANYTHING (invariant 6).

import { useQuery } from "@tanstack/react-query";

import {
  prerequisiteFromQuery,
  Prerequisites,
  type Prerequisite,
} from "../../components/prerequisites";
import { useT } from "../../i18n/context";
import type { ApiError, DocArticleListItem, Page } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { mayCheck, NOTIFICATION_READ, useNotificationRulesQuery } from "../../lib/detectors";
import { useMePermissions, type MePermissions } from "../../lib/mePermissions";
import { isPortalOnly } from "../../shell/nav";
import { AI_STATUS_KEY } from "../ai-admin/api";
import type { AiStatus } from "../ai-admin/types";
import { useRoles } from "../settings/api";
import { settingsHref } from "../settings/tabs";

const ROLES_READ = "roles.read";
const DOCS_READ = "docs.read";
const AI_USE = "ai.use";

/** The seeded global role a portal account holds (`0028_portal.py`). */
export const REQUESTER_ROLE = "requester";

export function PortalPrerequisites() {
  const me = useMePermissions();
  if (me.status !== "ready" || isPortalOnly(me.codes)) return null;
  return <OperatorPortalPrerequisites me={me} />;
}

function OperatorPortalPrerequisites({ me }: { me: MePermissions }) {
  const t = useT();
  const api = useApi();
  const roles = useRoles(mayCheck(me, ROLES_READ));
  const articles = useQuery<Page<DocArticleListItem>, ApiError>({
    queryKey: ["doc-articles"],
    queryFn: () => api.listArticles(),
    enabled: mayCheck(me, DOCS_READ),
    retry: false,
  });
  const aiStatus = useQuery<AiStatus, ApiError>({
    queryKey: [AI_STATUS_KEY],
    queryFn: () => api.getAiStatus(),
    enabled: mayCheck(me, AI_USE),
    retry: false,
  });
  const rules = useNotificationRulesQuery();

  const requesterCount = (list: { roles: { name: string; assigned_users: number }[] }) =>
    list.roles.filter((role) => role.name === REQUESTER_ROLE).reduce((sum, role) => sum + role.assigned_users, 0);

  const requestersItem = prerequisiteFromQuery({
    label: t("portal.prerequisites.requesters.label"),
    query: roles,
    permission: { code: ROLES_READ, me },
    isDone: (list) => requesterCount(list) > 0,
    done: (list) => t("portal.prerequisites.requesters.done", { count: requesterCount(list) }),
    missing: {
      severity: "crit",
      detail: t("portal.prerequisites.requesters.missing"),
      cta: { to: settingsHref("users"), label: t("portal.prerequisites.requesters.cta") },
    },
  });

  // ONLY THE FIRST PAGE IS READ (the list serves 100 by default and has no
  // portal filter), so "none of these is published" is a finding only when
  // that page is the whole library. Past it, the row says it could not tell.
  const articlesItem: Prerequisite = (() => {
    const item = prerequisiteFromQuery({
      label: t("portal.prerequisites.articles.label"),
      query: articles,
      permission: { code: DOCS_READ, me },
      isDone: (page) => page.items.some((article) => article.portal_visible === true && !article.archived),
      done: (page) =>
        t("portal.prerequisites.articles.done", {
          count: page.items.filter((article) => article.portal_visible === true && !article.archived).length,
        }),
      missing: {
        severity: "warn",
        detail: t("portal.prerequisites.articles.missing"),
        cta: { to: "/docs", label: t("portal.prerequisites.articles.cta") },
      },
    });
    const page = articles.data;
    if (item.state === "missing" && page && page.total > page.items.length) {
      return {
        label: item.label,
        state: "unchecked",
        detail: t("portal.prerequisites.articles.unchecked", { count: page.items.length, shown: page.items.length, total: page.total }),
      };
    }
    return item;
  })();

  const assistantItem = prerequisiteFromQuery({
    label: t("portal.prerequisites.assistant.label"),
    query: aiStatus,
    permission: { code: AI_USE, me },
    isDone: (status) => status.enabled,
    done: t("portal.prerequisites.assistant.done"),
    missing: (status) => ({
      severity: "warn",
      detail: t("portal.prerequisites.assistant.missing", { reason: status.reason }),
    }),
  });

  const rulesItem = prerequisiteFromQuery({
    label: t("portal.prerequisites.rules.label"),
    query: rules,
    permission: { code: NOTIFICATION_READ, me },
    isDone: (list) => list.some((rule) => rule.active && rule.recipient_kind === "requester"),
    done: (list) =>
      t("portal.prerequisites.rules.done", {
        count: list.filter((rule) => rule.active && rule.recipient_kind === "requester").length,
      }),
    missing: {
      severity: "warn",
      detail: t("portal.prerequisites.rules.missing"),
      cta: { to: "/notifications#rules", label: t("portal.prerequisites.rules.cta") },
    },
  });

  return (
    <Prerequisites
      title={t("portal.prerequisites.title")}
      subtitle={t("portal.prerequisites.subtitle")}
      items={[requestersItem, articlesItem, assistantItem, rulesItem]}
    />
  );
}
