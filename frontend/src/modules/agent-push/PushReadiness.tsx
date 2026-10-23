// "Before a push can run": what is set up and what is not, with the way to fix
// each missing piece, at the top of both push install screens.
//
// THE OPERATOR ASKED FOR IT (2026-09-24): the page never said whether a
// credential for this purpose existed, so the first thing an operator learned
// about a missing one was a refusal. The first five pieces here are the ones
// an operator configures and the server refuses a push without:
//
//   1-3  a push credential, bound to a range, on an authorized range;
//   4    an enabled site server serving each authorized range -- the
//        preflight's `no_site_server` refusal (`agent_push/preflight.py`),
//        because every packet to an unenrolled machine must leave from a site
//        server on its segment, with no fallback (invariant 2a(a));
//   5    a published agent package for a platform a push installs onto --
//        dispatch's "no published agent release exists for {os}/{arch}"
//        (`agent_push/dispatch.py`): Linux and macOS over SSH, and Windows
//        over WinRM, each on 64-bit Intel/AMD or ARM.
//
// AND TWO MORE, FOR WINDOWS (design §2.4; D10 overturned 2026-09-28). A
// Windows push is WinRM with Kerberos only, so it also needs:
//
//   6    WinRM answering on the target (5986, or 5985 with Kerberos message
//        encryption) -- enabled by the Group Policy in deploy/WINDOWS-PUSH.md;
//   7    a domain controller reachable from the site server, because the
//        password is used only there, to get a ticket for the target.
//
// Both come from the credential-free WinRM probe, recorded on the host-key
// list. They are WARNINGS, not critical: a Linux or macOS push runs without
// either, and the server does not refuse on them -- a Windows push that lacks
// them fails at the site server instead, and saying so here is the point.
//
// Until 2026-09-28 this panel said a Windows package "does not count" and the
// screens said a Windows target is refused. Both stopped being true with D10.
//
// The remaining check (the target is an asset a person vouched for) is per
// machine and belongs to the push itself.
//
// This is the pattern `components/prerequisites.tsx` generalises, and it now
// renders through it. It KEEPS SHOWING WHEN EVERYTHING IS DONE: "all of it is
// in place" is itself what an operator opens these screens to find out.
//
// NOTHING HERE GATES ANYTHING (invariant 6). It reads lists the server already
// serves; the server refuses a push on its own checks whatever this panel says.

import type { ReactNode } from "react";

import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import { Trans } from "../../i18n/Trans";
import { combineQueries, prerequisiteFromQuery, Prerequisites } from "../../components/prerequisites";
import { joinWords, mayCheck, useInstallerPrerequisite, type ReleasePlatform } from "../../lib/detectors";
import { useMePermissions } from "../../lib/mePermissions";
import { useSiteServers } from "../site-servers/api";
import { SITE_READ } from "../site-servers/types";
import { usePushAuthorizations, usePushCredentialScopes, usePushCredentials, usePushProbeReadiness } from "./api";
import { grantStatus, kdcNotFound, scopeIsBound, siteServerCoverage, windowsProbeReadiness } from "./pushState";
import { AGENT_PUSH_RUN, AGENT_PUSH_SCOPE_MANAGE, WINDOWS_DOMAIN_CREDENTIAL_TYPE } from "./types";

export const NEW_PUSH_CREDENTIAL_HREF = "/credentials?new=push_install";

const CREDENTIALS_READ = "credentials.read";

/**
 * What dispatch accepts: Linux and macOS over SSH, Windows over WinRM
 * (Kerberos only), each amd64 or arm64.
 */
export const PUSH_PLATFORMS: readonly ReleasePlatform[] = [
  { os: "linux", arch: "amd64" },
  { os: "linux", arch: "arm64" },
  { os: "darwin", arch: "amd64" },
  { os: "darwin", arch: "arm64" },
  { os: "windows", arch: "amd64" },
  { os: "windows", arch: "arm64" },
];

/** Where the one-time Windows setup is written down. A file path, so monospace. */
const RUNBOOK_TAGS = { code: <code className="mono" /> };

/** A catalogue sentence that names the runbook, with the path in monospace. */
function WithRunbook({ k, vars }: { k: MessageKey; vars?: Record<string, string | number> }) {
  return <Trans k={k} vars={vars} components={RUNBOOK_TAGS} />;
}

export function PushReadiness() {
  const t = useT();
  const me = useMePermissions();
  const credentials = usePushCredentials();
  const scopes = usePushCredentialScopes(false);
  const authorizations = usePushAuthorizations(false);
  const siteServers = useSiteServers({ enabled: true }, { enabled: mayCheck(me, SITE_READ) });
  const probes = usePushProbeReadiness();

  // Only what a push would actually accept: an expired grant is still listed
  // (the table shows it) but authorizes nothing, and neither does an unbound
  // scope (Codex review on #87).
  const activeGrants = (authorizations.data ?? []).filter((grant) => grantStatus(grant) === "active");

  const credentialItem = prerequisiteFromQuery({
    label: t("agentPush.readiness.credential.label"),
    query: credentials,
    permission: { code: CREDENTIALS_READ, me },
    isDone: (list) => list.length > 0,
    // THE DOMAIN ADMIN WARNING STANDS; IT DOES NOT REFUSE (design §0 item 4,
    // §2.3 "What remains"). The operator chose an existing Domain Admin for
    // Windows push (Q7). Kerberos keeps the password off the target, but the
    // site server holds it in memory for the length of a push, and this
    // console cannot see what rights an account holds in the directory -- so
    // it says what the choice means, every time, rather than pretending to
    // check it.
    done: (list) =>
      t("agentPush.readiness.credential.done", { count: list.length }) +
      (list.some((credential) => credential.type === WINDOWS_DOMAIN_CREDENTIAL_TYPE)
        ? ` ${t("agentPush.readiness.credential.doneWindows")}`
        : ""),
    missing: {
      severity: "crit",
      detail: t("agentPush.readiness.credential.missing"),
      cta: { to: NEW_PUSH_CREDENTIAL_HREF, label: t("agentPush.readiness.credential.cta") },
    },
  });

  // The scope list is readable without `credentials.read`, so an unreadable
  // credential list is not a reason to stop checking assignments -- only a
  // credential list that loaded and was genuinely empty is.
  const noCredentialYet = credentials.isSuccess && credentials.data.length === 0;
  const scopeItem = prerequisiteFromQuery({
    label: t("agentPush.readiness.scope.label"),
    query: scopes,
    permission: { code: AGENT_PUSH_SCOPE_MANAGE, me },
    waitingOn: noCredentialYet ? t("agentPush.readiness.scope.waiting") : null,
    isDone: (list) => list.some(scopeIsBound),
    done: (list) => t("agentPush.readiness.scope.done", { count: list.filter(scopeIsBound).length }),
    missing: {
      severity: "warn",
      detail: t("agentPush.readiness.scope.missing"),
      cta: { to: "/agent-push/credential-scopes", label: t("agentPush.readiness.scope.cta") },
    },
  });

  const authorizationItem = prerequisiteFromQuery({
    label: t("agentPush.readiness.authorization.label"),
    query: authorizations,
    permission: { code: AGENT_PUSH_SCOPE_MANAGE, me },
    isDone: () => activeGrants.length > 0,
    done: () => t("agentPush.readiness.authorization.done", { count: activeGrants.length }),
    missing: {
      severity: "warn",
      detail: t("agentPush.readiness.authorization.missing"),
      cta: { to: "/agent-push/authorizations#authorize", label: t("agentPush.readiness.authorization.cta") },
    },
  });

  // Checked per authorized range, by the rule the preflight applies per
  // address (`siteServerCoverage`). An account that cannot read the grants
  // gets the grants' refusal, which the helper reads as "cannot be checked
  // from this account".
  const siteServerItem = prerequisiteFromQuery({
    label: t("agentPush.readiness.siteServer.label"),
    query: combineQueries(authorizations, siteServers),
    permission: { code: SITE_READ, me },
    waitingOn: authorizations.isSuccess && activeGrants.length === 0 ? t("agentPush.readiness.siteServer.waiting") : null,
    isDone: ([, servers]) => activeGrants.every((grant) => siteServerCoverage(grant.prefix, servers) === "served"),
    done: ([, servers]) => t("agentPush.readiness.siteServer.done", { count: servers.length }),
    missing: ([, servers]) => {
      const by = (coverage: string) =>
        activeGrants.filter((grant) => siteServerCoverage(grant.prefix, servers) === coverage).map((g) => g.prefix);
      const none = by("none");
      const partial = by("partial");
      const ipv6 = by("ipv6");
      const sentences = [
        none.length > 0 ? t("agentPush.readiness.siteServer.none", { ranges: joinWords(none) }) : "",
        partial.length > 0
          ? t("agentPush.readiness.siteServer.partial", { count: partial.length, ranges: joinWords(partial) })
          : "",
        ipv6.length > 0
          ? t("agentPush.readiness.siteServer.ipv6", { count: ipv6.length, ranges: joinWords(ipv6) })
          : "",
        t("agentPush.readiness.siteServer.outside"),
      ];
      return {
        severity: "crit",
        detail: sentences.filter(Boolean).join(" "),
        cta: { to: "/site-servers", label: t("agentPush.readiness.siteServer.cta") },
      };
    },
  });

  const releaseItem = useInstallerPrerequisite({
    label: t("agentPush.readiness.release.label"),
    platforms: PUSH_PLATFORMS,
    severity: "crit",
    missingDetail: t("agentPush.readiness.release.missing"),
  });

  const notProbed = (why: string): ReactNode => <WithRunbook k="agentPush.readiness.winrm.notProbed" vars={{ why }} />;

  // Design §2.4: done when WinRM answered on any probed Windows machine. The
  // per-machine answer is the push's; this says whether the estate is set up.
  const winrmItem = prerequisiteFromQuery({
    label: t("agentPush.readiness.winrm.label"),
    query: probes,
    permission: { code: AGENT_PUSH_RUN, me },
    isDone: (rows) => windowsProbeReadiness(rows).winrm > 0,
    done: (rows) => {
      const r = windowsProbeReadiness(rows);
      return (
        <>
          {t("agentPush.readiness.winrm.done", { count: r.probed, winrm: r.winrm, https: r.https, http: r.http })}
          {r.winrm < r.probed ? (
            <>
              {" "}
              <WithRunbook k="agentPush.readiness.winrm.others" />
            </>
          ) : null}{" "}
          <WithRunbook k="agentPush.readiness.winrm.untested" />
        </>
      );
    },
    missing: (rows) => {
      const r = windowsProbeReadiness(rows);
      return {
        severity: "warn",
        detail:
          r.probed === 0 ? (
            notProbed(t("agentPush.readiness.winrm.enableFirst"))
          ) : (
            <WithRunbook k="agentPush.readiness.winrm.none" vars={{ count: r.probed }} />
          ),
      };
    },
  });

  // The password is used only against the domain controller (design §2.3), so
  // a site server that cannot reach one cannot push to Windows at all.
  const kdcItem = prerequisiteFromQuery({
    label: t("agentPush.readiness.kdc.label"),
    query: probes,
    permission: { code: AGENT_PUSH_RUN, me },
    isDone: (rows) => windowsProbeReadiness(rows).kdc > 0,
    done: (rows) => {
      const r = windowsProbeReadiness(rows);
      return t("agentPush.readiness.kdc.done", { count: r.probed, kdc: r.kdc });
    },
    missing: (rows) => {
      const r = windowsProbeReadiness(rows);
      return {
        severity: "warn",
        detail:
          r.probed === 0 ? (
            notProbed(t("agentPush.readiness.kdc.kerberosNeeds"))
          ) : (
            <WithRunbook k="agentPush.readiness.kdc.missing" vars={{ count: r.probed, state: kdcNotFound() }} />
          ),
      };
    },
  });

  return (
    <Prerequisites
      title={t("agentPush.readiness.title")}
      subtitle={t("agentPush.readiness.subtitle")}
      items={[credentialItem, scopeItem, authorizationItem, siteServerItem, releaseItem, winrmItem, kdcItem]}
      showWhenDone
    />
  );
}
