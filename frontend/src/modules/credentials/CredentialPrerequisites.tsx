// "Before a saved credential can be used": the one cross-module thing a saved
// credential can still be missing (`docs/design/cross-module-guidance.md` §2,
// Credentials -- a reverse link, advisory).
//
// A push install credential is presented only on the address ranges it is
// assigned to (invariant 2a(d)), so one that is saved and assigned nowhere is
// used nowhere. Nothing else on this page depends on another module, so the
// panel exists only while a push install credential does.
//
// NOTHING HERE GATES ANYTHING (invariant 6).

import { useQuery } from "@tanstack/react-query";

import { useT } from "../../i18n/context";

import { prerequisiteFromQuery, Prerequisites } from "../../components/prerequisites";
import type { ApiError, Credential } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { joinWords, mayCheck } from "../../lib/detectors";
import { useMePermissions } from "../../lib/mePermissions";
import { PUSH_CREDENTIAL_SCOPES_KEY } from "../agent-push/api";
import { scopeIsBound } from "../agent-push/pushState";
import { AGENT_PUSH_SCOPE_MANAGE, PUSHABLE_CREDENTIAL_TYPES, type PushCredentialScopeOut } from "../agent-push/types";

export function CredentialPrerequisites({ credentials }: { credentials: readonly Credential[] | undefined }) {
  const t = useT();
  const api = useApi();
  const me = useMePermissions();
  // Both push credential types: an SSH `push_install` account and, since D10
  // was overturned (2026-09-28), a `windows_domain` account for WinRM.
  const pushCredentials = (credentials ?? []).filter((credential) =>
    PUSHABLE_CREDENTIAL_TYPES.includes(credential.type),
  );
  // The push install screens' own key for the assigned-only list.
  const scopes = useQuery<PushCredentialScopeOut[], ApiError>({
    queryKey: [PUSH_CREDENTIAL_SCOPES_KEY, false],
    queryFn: () => api.listPushCredentialScopes(false),
    enabled: pushCredentials.length > 0 && mayCheck(me, AGENT_PUSH_SCOPE_MANAGE),
    retry: false,
  });

  if (pushCredentials.length === 0) return null;

  const unassigned = (list: readonly PushCredentialScopeOut[]) => {
    const bound = new Set(list.filter(scopeIsBound).map((scope) => scope.credential_id));
    return pushCredentials.filter((credential) => !bound.has(credential.id));
  };

  const scopeItem = prerequisiteFromQuery({
    label: t("credentials.prerequisites.label"),
    query: scopes,
    permission: { code: AGENT_PUSH_SCOPE_MANAGE, me },
    isDone: (list) => unassigned(list).length === 0,
    done: t("credentials.prerequisites.done"),
    missing: (list) => {
      const names = unassigned(list).map((credential) => credential.name);
      return {
        severity: "warn",
        detail: t("credentials.prerequisites.unassigned", { names: joinWords(names), count: names.length }),
        cta: {
          to: "/agent-push/credential-scopes",
          label: names.length === 1 ? t("credentials.prerequisites.ctaOne") : t("credentials.prerequisites.ctaMany"),
        },
      };
    },
  });

  return <Prerequisites title={t("credentials.prerequisites.title")} items={[scopeItem]} />;
}
