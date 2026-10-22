// AI connections — the organization's MCP switch (`mcp_enabled` over
// `PATCH /orgs/current`, `orgs.manage`), the address an AI app connects to,
// a short guide, and the apps that used it here
// (`GET /mcp/connections`, `POST /mcp/connections/{client_id}/revoke`, both
// `mcp.use`; a revoke for everyone also needs `orgs.manage`). MCP server
// design §8.
//
// TWO PERMISSIONS, AND THE TAB IS OFFERED FOR EITHER. An org-admin who does
// not use AI apps still owns the switch, and a person who uses one still owns
// their own revoke. So the switch reads the ORG (any operator may read it) and
// is editable with `orgs.manage`; the address and the apps are the
// connections read, which is only sent with `mcp.use` — without it the rest
// of the tab is read-only and says which permission it needs, rather than
// firing a request that can only 403. None of this is security (invariant 6):
// the server refuses every one of these calls on its own.
//
// THE SWITCH WRITES ONE KEY. The PATCH body is `{settings: {mcp_enabled}}`
// and nothing else: the server merges settings key by key, so the rest of the
// org document — and the org name — are never re-sent from this tab.
//
// TURNING IT ON ASKS FIRST; TURNING IT OFF DOES NOT. On opens a remote path
// into the organization (invariant 1c): from then on an AI app can act as any
// person who connects it, within that person's permissions. Off only closes
// that path, and a question in front of closing it would slow the one move an
// administrator makes in a hurry. This is also why the key has no second
// editor on the Organization tab (`SWITCHES_WITH_OWN_TAB` in ./types.ts): a
// generic toggle there would be the same write without the question.
//
// A REVOKE IS IN FORCE WHEN IT RETURNS, AND THE SCREEN SAYS WHAT IS NOT. The
// server records the revocation first (every token the app already holds is
// refused from its next call) and then withdraws each person's consent at the
// identity provider, best effort. `keycloak_failures` counts the people for
// whom that second half failed: for them the app's refresh token could still
// obtain a NEW token, so the outcome says the app "may still reconnect without
// asking" and asks for another try — never "access ends soon", which would be
// a promise the platform cannot keep. The result is held as data on the
// mutation and worded at render, so a language switch re-words it.
//
// NO VENDOR IS THE ONLY OPTION. The guide names Claude as an example of an AI
// app, and the steps are what any MCP client with custom connectors asks for.

import { useId, useState, type CSSProperties } from "react";

import { ConfirmDialog } from "../../components/confirm";
import { describeSubmitError } from "../../components/form";
import { Button, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { lazyLabels } from "../../i18n/lazy";
import type { McpAppOut } from "../../lib/api";
import { formatDateTime } from "../../lib/datetime";
import { KIND_ICON, Panel, QueryState, Toggle } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { KV } from "../../shell/viz";
import { useCurrentOrg, useMcpConnections, useMe, useRevokeMcpConnection, useUpdateCurrentOrgMutation } from "./api";
import { LoadFailure } from "./LoadFailure";
import { ORGS_MANAGE } from "./OrganizationTab";
import { isForbidden, permissionReason } from "./settingsState";

export const MCP_USE = "mcp.use";

/** The apps table's column heads, read from the catalogue when drawn. */
const COLUMNS = lazyLabels({
  app: "settings.aiConnections.apps.columns.app",
  people: "settings.aiConnections.apps.columns.people",
  lastUsed: "settings.aiConnections.apps.columns.lastUsed",
  actions: "settings.aiConnections.apps.columns.actions",
});

/** A subhead inside the panel: the body rung at 600, as on the Assistant tab. */
const SUBHEAD: CSSProperties = { fontSize: "var(--t-body)", fontWeight: 600, margin: "16px 0 6px" };
/** Tailwind's preflight removes list markers; the steps are numbered, so they are declared. */
const STEPS: CSSProperties = { listStyle: "decimal outside", paddingLeft: 20, margin: 0 };
const STEP: CSSProperties = { marginTop: 4 };
/** A URL has no spaces to break at; on a phone it would run out of the panel. */
const ADDRESS: CSSProperties = { overflowWrap: "anywhere" };

export function AiConnectionsTab({ canUse, canManageOrg }: { canUse: boolean; canManageOrg: boolean }) {
  const t = useT();
  const me = useMe();
  const org = useCurrentOrg();
  const update = useUpdateCurrentOrgMutation();
  const connections = useMcpConnections(canUse);
  const revoke = useRevokeMcpConnection();
  const guideId = useId();
  /** The app a "Revoke for everyone" is waiting to be confirmed for. */
  const [confirming, setConfirming] = useState<string | null>(null);
  /** Turning the switch ON, waiting to be confirmed. */
  const [enabling, setEnabling] = useState(false);

  // What the server stored, and nothing else: `mcp_enabled` is off unless it
  // is literally `true`, which is how the MCP server reads it too.
  const enabled = org.data?.settings?.["mcp_enabled"] === true;
  const switchEditable = canManageOrg && org.data != null && !update.isPending;
  const connectionsForbidden = isForbidden(connections.error);
  const readOnly = t("settings.aiConnections.readOnly", { code: MCP_USE });

  function revokeFor(clientId: string, scope: "me" | "everyone") {
    revoke.mutate({ clientId, scope });
  }

  return (
    <>
      <Panel title={t("settings.aiConnections.title")} subtitle={t("settings.aiConnections.subtitle")}>
        {org.error != null ? <LoadFailure error={org.error} /> : null}

        <Toggle
          label={t("settings.aiConnections.switch")}
          checked={enabled}
          disabled={!switchEditable}
          onChange={(next) => (next ? setEnabling(true) : update.mutate({ settings: { mcp_enabled: false } }))}
        />
        {!canManageOrg ? (
          <p className="form-hint" style={{ marginTop: 6 }}>
            {permissionReason(ORGS_MANAGE)}
          </p>
        ) : null}
        {update.error ? (
          <p role="alert" className="form-error" style={{ marginTop: 6 }}>
            <Icon name={KIND_ICON.crit} size={13} />
            {describeSubmitError(update.error)}
          </p>
        ) : null}
        <p className="note" style={{ marginTop: 8 }}>
          {t("settings.aiConnections.switchNote")}
        </p>

        <h3 style={SUBHEAD}>{t("settings.aiConnections.addressHeading")}</h3>
        {!canUse || connectionsForbidden ? (
          <p className="note">{readOnly}</p>
        ) : connections.isLoading ? (
          <p className="note">{t("settings.aiConnections.loading")}</p>
        ) : connections.error != null ? (
          <LoadFailure error={connections.error} />
        ) : connections.data?.address ? (
          <KV
            items={[
              [t("settings.aiConnections.address"), <span className="mono" style={ADDRESS}>{connections.data.address}</span>],
            ]}
          />
        ) : (
          <p role="status" className="notice gold">
            <Icon name={KIND_ICON.warn} size={16} />
            <span>{t("settings.aiConnections.addressUnset")}</span>
          </p>
        )}

        <h3 id={guideId} style={SUBHEAD}>
          {t("settings.aiConnections.guideHeading")}
        </h3>
        <ol className="note" style={STEPS} aria-labelledby={guideId}>
          <li style={STEP}>{t("settings.aiConnections.steps.add")}</li>
          <li style={STEP}>{t("settings.aiConnections.steps.signIn")}</li>
          <li style={STEP}>{t("settings.aiConnections.steps.tools")}</li>
        </ol>
      </Panel>

      <RevokeOutcome
        pending={revoke.isPending}
        error={revoke.error}
        clientId={revoke.variables?.clientId}
        scope={revoke.variables?.scope}
        failures={revoke.data?.keycloak_failures}
      />

      {canUse && connections.data ? (
        <Panel
          title={t("settings.aiConnections.apps.title")}
          subtitle={t("settings.aiConnections.apps.subtitle")}
          bare
        >
          <QueryState
            loading={false}
            error={null}
            empty={connections.data.apps.length === 0}
            emptyLabel={t("settings.aiConnections.apps.empty")}
            emptyIcon="sparkles"
          >
            <Table>
              <thead>
                <tr>
                  <Th>{COLUMNS.app}</Th>
                  <Th>{COLUMNS.people}</Th>
                  <Th>{COLUMNS.lastUsed}</Th>
                  <Th>{COLUMNS.actions}</Th>
                </tr>
              </thead>
              <tbody>
                {connections.data.apps.map((app) => (
                  <AppRow
                    key={app.client_id}
                    app={app}
                    timezone={me.data?.timezone}
                    canRevokeForEveryone={canManageOrg}
                    busy={revoke.isPending}
                    onRevokeForMe={() => revokeFor(app.client_id, "me")}
                    onRevokeForEveryone={() => setConfirming(app.client_id)}
                  />
                ))}
              </tbody>
            </Table>
          </QueryState>
        </Panel>
      ) : null}

      <ConfirmDialog
        open={enabling}
        title={t("settings.aiConnections.enable.title")}
        subject={org.data?.name ?? ""}
        body={t("settings.aiConnections.enable.body", { code: MCP_USE })}
        confirmLabel={t("settings.aiConnections.enable.action")}
        onConfirm={() => {
          setEnabling(false);
          update.mutate({ settings: { mcp_enabled: true } });
        }}
        onCancel={() => setEnabling(false)}
      />

      <ConfirmDialog
        open={confirming != null}
        title={t("settings.aiConnections.confirm.title")}
        subject={confirming ?? ""}
        body={t("settings.aiConnections.confirm.body")}
        confirmLabel={t("settings.aiConnections.confirm.action")}
        destructive
        onConfirm={() => {
          const target = confirming;
          setConfirming(null);
          if (target != null) revokeFor(target, "everyone");
        }}
        onCancel={() => setConfirming(null)}
      />
    </>
  );
}

function AppRow({
  app,
  timezone,
  canRevokeForEveryone,
  busy,
  onRevokeForMe,
  onRevokeForEveryone,
}: {
  app: McpAppOut;
  timezone: string | null | undefined;
  canRevokeForEveryone: boolean;
  busy: boolean;
  onRevokeForMe: () => void;
  onRevokeForEveryone: () => void;
}) {
  const t = useT();
  // "For everyone" covers "for you", so only the wider one is said.
  const state = app.revoked_for_everyone
    ? t("settings.aiConnections.apps.revokedForEveryone")
    : app.revoked_for_me
      ? t("settings.aiConnections.apps.revokedForMe")
      : null;
  return (
    <tr>
      <Td lead={<Icon name="sparkles" size={18} />}>
        {/* The client id is what the app registered: recorded, not translated. */}
        <div className="cell-title">{app.client_id}</div>
        {state ? <div className="cell-sub">{state}</div> : null}
      </Td>
      <Td>
        <span className="count">{app.people}</span>
      </Td>
      <Td>{formatDateTime(app.last_used_at, timezone)}</Td>
      <Td>
        {/* A revoke is offered again after one: it is how a revoke whose
            sign-in half failed for some people is retried. */}
        <div className="flex flex-wrap gap-2">
          <Button
            type="button"
            variant="outline"
            aria-label={t("settings.aiConnections.apps.revokeMeAria", { app: app.client_id })}
            disabled={busy}
            onClick={onRevokeForMe}
          >
            {t("settings.aiConnections.apps.revokeMe")}
          </Button>
          {canRevokeForEveryone ? (
            <Button
              type="button"
              variant="outline"
              className="danger"
              aria-label={t("settings.aiConnections.apps.revokeEveryoneAria", { app: app.client_id })}
              disabled={busy}
              onClick={onRevokeForEveryone}
            >
              {t("settings.aiConnections.apps.revokeEveryone")}
            </Button>
          ) : null}
        </div>
      </Td>
    </tr>
  );
}

/**
 * What the last revoke did, worded from the data the server returned.
 *
 * Nothing while one is in flight: an outcome from the previous revoke beside a
 * request that has not answered yet would describe the wrong act.
 */
function RevokeOutcome({
  pending,
  error,
  clientId,
  scope,
  failures,
}: {
  pending: boolean;
  error: unknown;
  clientId: string | undefined;
  scope: "me" | "everyone" | undefined;
  failures: number | undefined;
}) {
  const t = useT();
  if (pending || clientId === undefined || scope === undefined) return null;
  if (error != null) {
    return (
      <p role="alert" className="notice red" style={{ margin: "16px 0" }}>
        <Icon name={KIND_ICON.crit} size={16} />
        <span>{describeSubmitError(error)}</span>
      </p>
    );
  }
  if (failures === undefined) return null;
  if (failures > 0) {
    return (
      <p role="alert" className="notice gold" style={{ margin: "16px 0" }}>
        <Icon name={KIND_ICON.warn} size={16} />
        <span>
          {/* "For you" when it was your own revoke: a count of one person
              would be a stranger's way of saying it. */}
          {scope === "me"
            ? t("settings.aiConnections.partialMe", { app: clientId })
            : t("settings.aiConnections.partial", { app: clientId, count: failures })}
        </span>
      </p>
    );
  }
  return (
    <p role="status" className="notice" style={{ margin: "16px 0" }}>
      <Icon name={KIND_ICON.ok} size={16} />
      <span>
        {scope === "everyone"
          ? t("settings.aiConnections.revoked.everyone", { app: clientId })
          : t("settings.aiConnections.revoked.me", { app: clientId })}
      </span>
    </p>
  );
}
