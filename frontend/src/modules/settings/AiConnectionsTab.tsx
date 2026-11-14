// AI connections — the organization's MCP switch (`mcp_enabled` over
// `PATCH /orgs/current`, `orgs.manage`), the address an AI app connects to,
// a short guide, and the apps that used it here
// (`GET /mcp/connections`, `POST /mcp/connections/{app_id}/revoke`, both
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
// A RECENT SECOND FACTOR TO TURN IT ON (design §2.5, operator 2026-11-08, S3),
// never to turn it off (S4): the server answers 403 `step_up_required`, and
// `useStepUp` asks for the factor after the question below and sends the
// switch once more.
//
// TURNING IT ON ASKS FIRST; TURNING IT OFF DOES NOT. On opens a remote path
// into the organization (invariant 1c): from then on an AI app can act as any
// person who connects it, within that person's permissions. Off only closes
// that path, and a question in front of closing it would slow the one move an
// administrator makes in a hurry. This is also why the key has no second
// editor on the Organization tab (`SWITCHES_WITH_OWN_TAB` in ./types.ts): a
// generic toggle there would be the same write without the question.
//
// A REVOKE IS IN FORCE WHEN IT RETURNS. The server records the revocation
// and ends every sign-in the app holds for whom it was revoked, in one
// transaction (own-auth plan 2026-11-06, D12): there is no second half that
// can fail, so the screen never says "may still reconnect". The app is
// named by its registration's id; one that has none any more holds no
// access and offers no revoke.
//
// NO VENDOR IS THE ONLY OPTION. The guide names Claude as an example of an AI
// app, and the steps are what any MCP client with custom connectors asks for.

import { useId, useState, type CSSProperties } from "react";

import { ConfirmDialog } from "../../components/confirm";
import { describeSubmitError } from "../../components/form";
import { useStepUp } from "../../components/stepUp";
import { Button, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { lazyLabels } from "../../i18n/lazy";
import type { McpAppOut } from "../../lib/api";
import { formatDateTime } from "../../lib/datetime";
import { KIND_ICON, Panel, QueryState, Toggle } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { KV } from "../../shell/viz";
import { useCurrentOrg, useMcpConnections, useMe, useRevokeMcpConnection, useUpdateCurrentOrgMutation, useGlobalWriteReason } from "./api";
import { LoadFailure } from "./LoadFailure";
import { ORGS_MANAGE } from "./OrganizationTab";
import { isForbidden } from "./settingsState";

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

/** `canSwitch`: the MCP switch is an org setting (`PATCH /orgs/current`), so `orgs.manage` on every client; revoking for everyone is not a global route and keeps `canManageOrg`. */
export function AiConnectionsTab({
  canUse,
  canManageOrg,
  canSwitch = canManageOrg,
}: {
  canUse: boolean;
  canManageOrg: boolean;
  canSwitch?: boolean;
}) {
  const t = useT();
  const reason = useGlobalWriteReason(ORGS_MANAGE);
  const me = useMe();
  const org = useCurrentOrg();
  const stepUp = useStepUp();
  const update = useUpdateCurrentOrgMutation(stepUp.run);
  const connections = useMcpConnections(canUse);
  const revoke = useRevokeMcpConnection();
  const guideId = useId();
  /** The app a "Revoke for everyone" is waiting to be confirmed for. */
  const [confirming, setConfirming] = useState<McpAppOut | null>(null);
  /** Turning the switch ON, waiting to be confirmed. */
  const [enabling, setEnabling] = useState(false);

  // What the server stored, and nothing else: `mcp_enabled` is off unless it
  // is literally `true`, which is how the MCP server reads it too.
  const enabled = org.data?.settings?.["mcp_enabled"] === true;
  const switchEditable = canSwitch && org.data != null && !update.isPending;
  const connectionsForbidden = isForbidden(connections.error);
  const readOnly = t("settings.aiConnections.readOnly", { code: MCP_USE });

  function revokeFor(app: McpAppOut, scope: "me" | "everyone") {
    if (app.id !== null) revoke.mutate({ appId: app.id, label: app.host ?? app.client_id, scope });
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
        {!canSwitch ? (
          <p className="form-hint" style={{ marginTop: 6 }}>
            {reason}
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
        label={revoke.variables?.label}
        scope={revoke.variables?.scope}
        done={revoke.data !== undefined}
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
                    onRevokeForMe={() => revokeFor(app, "me")}
                    onRevokeForEveryone={() => setConfirming(app)}
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
        subject={confirming ? (confirming.host ?? confirming.client_id) : ""}
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

      {/* Outside every form: the recent confirmation holds forms of its own. */}
      {stepUp.dialog}
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
  // The host is what the app cannot fake (B3-10); without a registration, its client id.
  const label = app.host ?? app.client_id;
  // "For everyone" covers "for you", so only the wider one is said.
  const state = app.revoked_for_everyone
    ? t("settings.aiConnections.apps.revokedForEveryone")
    : app.revoked_for_me
      ? t("settings.aiConnections.apps.revokedForMe")
      : null;
  return (
    <tr>
      <Td lead={<Icon name="sparkles" size={18} />}>
        {/* The host and the client id are recorded, not translated; the name is the app's own claim. */}
        <div className="cell-title">{label}</div>
        {app.name !== null ? (
          <div className="cell-sub">{t("settings.aiConnections.apps.calledItself", { name: app.name })}</div>
        ) : null}
        {app.kind === "registered" ? (
          <div className="cell-sub">{t("settings.aiConnections.apps.selfRegistered")}</div>
        ) : null}
        {app.host !== null ? <div className="cell-sub mono">{app.client_id}</div> : null}
        {state ? <div className="cell-sub">{state}</div> : null}
        {app.id === null ? <div className="cell-sub">{t("settings.aiConnections.apps.unregistered")}</div> : null}
      </Td>
      <Td>
        <span className="count">{app.people}</span>
      </Td>
      <Td>{formatDateTime(app.last_used_at, timezone)}</Td>
      <Td>
        {/* A revoke is offered again after one: an app that reconnected since
            is revoked again the same way. */}
        {app.id !== null ? (
          <div className="flex flex-wrap gap-2">
            <Button
              type="button"
              variant="outline"
              aria-label={t("settings.aiConnections.apps.revokeMeAria", { app: label })}
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
                aria-label={t("settings.aiConnections.apps.revokeEveryoneAria", { app: label })}
                disabled={busy}
                onClick={onRevokeForEveryone}
              >
                {t("settings.aiConnections.apps.revokeEveryone")}
              </Button>
            ) : null}
          </div>
        ) : null}
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
  label,
  scope,
  done,
}: {
  pending: boolean;
  error: unknown;
  label: string | undefined;
  scope: "me" | "everyone" | undefined;
  done: boolean;
}) {
  const t = useT();
  if (pending || label === undefined || scope === undefined) return null;
  if (error != null) {
    return (
      <p role="alert" className="notice red" style={{ margin: "16px 0" }}>
        <Icon name={KIND_ICON.crit} size={16} />
        <span>{describeSubmitError(error)}</span>
      </p>
    );
  }
  if (!done) return null;
  return (
    <p role="status" className="notice" style={{ margin: "16px 0" }}>
      <Icon name={KIND_ICON.ok} size={16} />
      <span>
        {scope === "everyone"
          ? t("settings.aiConnections.revoked.everyone", { app: label })
          : t("settings.aiConnections.revoked.me", { app: label })}
      </span>
    </p>
  );
}
