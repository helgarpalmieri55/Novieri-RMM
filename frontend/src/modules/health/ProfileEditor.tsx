import { useMemo, useState } from "react";
import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import { Trans } from "../../i18n/Trans";
import { Badge, Button, Table, Td, Th } from "../../components/ui";
import { ApiError } from "../../lib/api";
import { Panel } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { KIND_ICON, Pill } from "../../shell/viz";
import {
  useApprovedActions,
  useHealthCheckTypes,
  useHealthProfileChecks,
  useReplaceHealthChecksMutation,
} from "./api";
import {
  AUTONOMY_PRESENTATION,
  SEVERITY_PRESENTATION,
  checkGlyph,
  checkLabel,
  describeDuration,
  describeThreshold,
  formatSeconds,
  severityVariant,
  unansweredFamilies,
} from "./healthState";
import type { CheckIn, CheckOut, CheckTypeOut, OsFamily } from "./types";

// One profile's checks (spec §13): a table with the effective duration rendered
// in BOTH ticks and minutes, and the per-OS availability matrix inline.
//
// CHECKS ARE REPLACED AS A SET, NEVER INDIVIDUALLY, because that is what the
// API is: `PUT /health/profiles/{id}/checks`. Removing a row here and saving
// RETIRES that check server-side and clears its open alerts as
// `policy_removed`; the alert rows survive with a resolvable check id. The
// warning below says so before the operator saves, because "remove a row" looks
// reversible and its effect on open alerts is not.
//
// THE ROUND TRIP MUST BE LOSSLESS. `toCheckIn` carries `declares_notify`,
// `declares_open_ticket` and `remediate_approved_action_id` back out of every
// row it did not touch — a PUT that dropped them would silently re-arm a
// silent-by-design check (spec §7.3) and disarm every declared remediation.

export function ProfileEditor({
  profileId,
  canAdmin,
}: {
  profileId: string;
  canAdmin: boolean;
}) {
  const t = useT();
  const checks = useHealthProfileChecks(profileId);
  const checkTypes = useHealthCheckTypes();
  const approvedActions = useApprovedActions();
  const replace = useReplaceHealthChecksMutation();
  const [pendingRemovals, setPendingRemovals] = useState<string[]>([]);

  const typesByName = useMemo(() => {
    const map = new Map<string, CheckTypeOut>();
    for (const type of checkTypes.data ?? []) map.set(type.name, type);
    return map;
  }, [checkTypes.data]);

  const actionsById = useMemo(() => {
    const map = new Map<string, { key: string; autonomy: keyof typeof AUTONOMY_PRESENTATION }>();
    for (const action of approvedActions.data ?? []) {
      map.set(action.id, { key: action.key, autonomy: action.autonomy });
    }
    return map;
  }, [approvedActions.data]);

  // UNQUALIFIED. `instanceof ApiError` let a transport failure through to
  // `!checks.data`, which renders "Loading checks…" forever.
  if (checks.error) {
    const status = checks.error instanceof ApiError ? checks.error.status : null;
    return (
      <Panel title={t("health.editor.title")}>
        <p role="alert" className="note">
          {status === 403 ? t("health.editor.forbidden") : describeSubmitError(checks.error)}
        </p>
      </Panel>
    );
  }
  if (!checks.data) {
    return (
      <Panel title={t("health.editor.title")}>
        <div className="empty">
          <Icon name="clock" size={26} />
          <b>{t("health.editor.loading")}</b>
        </div>
      </Panel>
    );
  }

  const rows = checks.data.checks;
  const remaining = rows.filter((check) => !pendingRemovals.includes(check.id));

  return (
    <Panel
      title={t("health.editor.title")}
      subtitle={t("health.editor.subtitle", {
        version: checks.data.policy_version,
        every: formatSeconds(checks.data.tick_interval_s),
      })}
      bare={rows.length > 0}
    >
      {rows.length === 0 ? (
        <p className="note">{t("health.editor.empty")}</p>
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>{t("health.editor.colCheck")}</Th>
              <Th>{t("health.editor.colCondition")}</Th>
              <Th>{t("health.editor.colDuration")}</Th>
              <Th>{t("health.editor.colSeverity")}</Th>
              <Th>{t("health.editor.colAnswerable")}</Th>
              <Th>{t("health.editor.colDeclares")}</Th>
              <Th> </Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((check) => {
              const removed = pendingRemovals.includes(check.id);
              const type = typesByName.get(check.check_type);
              const gaps = unansweredFamilies(check);
              const remediation = check.remediate_approved_action_id
                ? actionsById.get(check.remediate_approved_action_id)
                : undefined;
              return (
                <tr key={check.id} className={removed ? "opacity-40 line-through" : ""}>
                  <Td lead={<Icon name={checkGlyph(check.check_type)} size={18} />}>
                    <div className="cell-title">
                      {checkLabel(check.check_type)}
                      {check.selector ? ` (${check.selector})` : ""}
                    </div>
                    <div className="cell-sub mono">{check.check_type}</div>
                    {!check.enabled && (
                      <div className="cell-sub">
                        <Badge variant="muted">{t("health.editor.disabled")}</Badge>
                      </div>
                    )}
                  </Td>
                  <Td>
                    <span className="mono">
                      {describeThreshold(check.comparator, check.threshold_display)}
                    </span>
                  </Td>
                  {/* Spec §0.3: BOTH units, with the profile's tick named, so
                      changing the tick interval visibly changes this. */}
                  <Td>{describeDuration(check)}</Td>
                  <Td>
                    <Badge variant={severityVariant(check.severity)}>
                      {SEVERITY_PRESENTATION[check.severity].label}
                    </Badge>
                  </Td>
                  <Td>
                    <span className="cell-sub">
                      {check.available_os_families.join(", ")}
                      {check.os_family ? t("health.editor.pinnedTo", { family: check.os_family }) : ""}
                    </span>
                    {/* The matrix's whole point: an operator must see BEFORE
                        saving which of their estate will report `unavailable`. */}
                    {gaps.length > 0 && (
                      <div className="mt-1">
                        <Badge variant="warning">
                          {t("health.editor.unavailableOn", { families: gaps.join(", ") })}
                        </Badge>
                      </div>
                    )}
                    {type?.evaluated_by === "server" && (
                      <div className="mt-1">
                        <Badge variant="info">{t("health.editor.evaluatedByServer")}</Badge>
                      </div>
                    )}
                  </Td>
                  <Td>
                    <div className="flex flex-wrap gap-1">
                      <Badge variant={check.declares_notify ? "info" : "muted"}>
                        {check.declares_notify ? t("health.editor.notify") : t("health.editor.silent")}
                      </Badge>
                      <Badge variant={check.declares_open_ticket ? "info" : "muted"}>
                        {check.declares_open_ticket ? t("health.editor.ticket") : t("health.editor.noTicket")}
                      </Badge>
                      {remediation ? (
                        <Badge variant={AUTONOMY_PRESENTATION[remediation.autonomy].variant}>
                          {t("health.editor.fix", { key: remediation.key })}
                        </Badge>
                      ) : (
                        <Badge variant="muted">{t("health.editor.noFix")}</Badge>
                      )}
                    </div>
                  </Td>
                  <Td>
                    {canAdmin && (
                      <Button
                        variant="outline"
                        onClick={() =>
                          setPendingRemovals((current) =>
                            removed
                              ? current.filter((id) => id !== check.id)
                              : [...current, check.id],
                          )
                        }
                      >
                        {removed ? t("health.editor.keep") : t("health.editor.remove")}
                      </Button>
                    )}
                  </Td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      )}

      {canAdmin && pendingRemovals.length > 0 && (
        <div className="notice gold" style={{ margin: 16 }}>
          <Icon name={KIND_ICON.warn} size={17} />
          {/* ONE BODY CHILD, because `.notice` is a flex row shaped
              icon-then-body. The sentence and the two buttons under it are
              one body, not two columns. */}
          <div>
            <p className="mb-2">
              <Trans
                k="health.editor.removalNotice"
                vars={{ count: pendingRemovals.length }}
                components={{ strong: <strong /> }}
              />
            </p>
            <div className="flex gap-2">
              <Button
                disabled={replace.isPending}
                onClick={() =>
                  replace.mutate({
                    id: profileId,
                    checks: remaining.map(toCheckIn),
                  })
                }
              >
                {t("health.editor.saveSet")}
              </Button>
              <Button variant="outline" onClick={() => setPendingRemovals([])}>
                {t("common.actions.cancel")}
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* SAVING A CHECK SET RETIRES CHECKS AND CLEARS THEIR OPEN ALERTS. A
          refusal that renders as an empty paragraph — which is what a bodyless
          503 did here — leaves the operator believing a destructive edit
          landed. */}
      {replace.error ? (
        <div role="alert" className="notice red" style={{ margin: 16 }}>
          <Icon name={KIND_ICON.crit} size={17} />
          <div>{t("health.editor.saveFailed", { detail: describeSubmitError(replace.error) })}</div>
        </div>
      ) : null}

      <CheckTypeMatrix types={checkTypes.data ?? []} />
    </Panel>
  );
}

/**
 * A stored check, as the shape the replace-set PUT accepts.
 *
 * `for_ticks` is sent and `for_seconds` is NOT: supplying both is refused
 * server-side ("two answers to one question"), and `for_ticks` is the stored
 * column, so a round trip through this function cannot change a duration.
 * The three declaration fields are carried through for the reason the module
 * docstring gives.
 */
export function toCheckIn(check: CheckOut): CheckIn {
  return {
    check_type: check.check_type,
    selector: check.selector,
    comparator: check.comparator,
    threshold_num: check.threshold_num,
    threshold_bool: check.threshold_bool,
    for_ticks: check.for_ticks,
    severity: check.severity,
    os_family: check.os_family,
    enabled: check.enabled,
    declares_notify: check.declares_notify,
    declares_open_ticket: check.declares_open_ticket,
    remediate_approved_action_id: check.remediate_approved_action_id,
  };
}

/**
 * The closed registry's per-OS availability matrix (spec §4.1, §13).
 *
 * Rendered as its own table rather than only per row, because the decision it
 * supports — "will this check answer on my estate?" — is made while choosing a
 * check type, before any row exists.
 */
export function CheckTypeMatrix({ types }: { types: readonly CheckTypeOut[] }) {
  const t = useT();
  const families: readonly OsFamily[] = ["windows", "linux", "macos"];
  if (types.length === 0) return null;
  return (
    <details style={{ padding: 16 }}>
      <summary className="note cursor-pointer">{t("health.editor.matrixSummary")}</summary>
      <Table>
        <thead>
          <tr>
            <Th>{t("health.editor.matrixType")}</Th>
            {families.map((family) => (
              <Th key={family}>{family}</Th>
            ))}
            <Th>{t("health.editor.matrixCadence")}</Th>
            <Th>{t("health.editor.matrixSelector")}</Th>
          </tr>
        </thead>
        <tbody>
          {types.map((type) => (
            <tr key={type.name}>
              <Td>{type.name}</Td>
              {families.map((family) => (
                <Td key={family}>
                  {type.os_families.includes(family) ? (
                    <span aria-label={t("health.editor.answersOn", { type: type.name, family })}>
                      <Pill kind="ok">{t("health.editor.yes")}</Pill>
                    </span>
                  ) : (
                    <span aria-label={t("health.editor.unavailableOnFamily", { type: type.name, family })}>
                      <Pill kind="warn">{t("health.editor.unavailable")}</Pill>
                    </span>
                  )}
                </Td>
              ))}
              <Td>
                {type.evaluated_by === "server"
                  ? t("health.editor.cadenceServer")
                  : type.cadence === "posture"
                    ? t("health.editor.cadencePosture")
                    : t("health.editor.cadenceLive")}
              </Td>
              <Td>{type.selector_meaning || "—"}</Td>
            </tr>
          ))}
        </tbody>
      </Table>
    </details>
  );
}
