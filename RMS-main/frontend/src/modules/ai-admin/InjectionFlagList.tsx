// §3.3's "for admin review": the flagged excerpts, org-wide.
//
// **THE EXCERPTS ARE ATTACKER-CONTROLLED PROSE FROM UNAUTHENTICATED SOURCES**,
// which is why the route behind this list is `ai.admin` and not `ai.use`. They
// are rendered as PLAIN TEXT, never through the markdown renderer: there is no
// reason for an injection attempt to be formatted, and a renderer is a surface
// where there does not need to be one.
//
// Drawn as v3's bare panel of rows: the matched rule as a warning pill (the
// glyph carries the state, not only the colour), the record it came from as
// the row's title with the tool as its sub-line, and the excerpt in mono.

import { describeSubmitError } from "../../components/form";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { Icon } from "../../shell/icons";
import { humaniseEnum, humaniseStatus } from "../../lib/labels";
import { KIND_ICON, Panel, QueryState, StatusPill } from "../../shell/components";
import { useAiInjectionFlags } from "./api";
import type { AiInjectionRule, AiSourceType, AiTool } from "./types";

/**
 * The detector's rule names, in words.
 *
 * `matched_rule` is `app/core/ai/detect.py::_RULES`' key — the server's own
 * vocabulary, and a table of flagged content is read by a person deciding
 * whether an excerpt matters, not by the detector. The wire value stays the
 * pill's `value` (so its classification, its tests and any future styling
 * still key off the identifier); only the LABEL is translated.
 *
 * A rule this list does not name is humanised rather than dropped: a detector
 * that gains a rule must not produce a blank cell here, and it must not go on
 * printing `snake_case` at an administrator either.
 */
const RULES: ReadonlySet<string> = new Set<AiInjectionRule>([
  "redirection",
  "role_assertion",
  "tool_coercion",
  "nonce_forgery",
]);
const TOOLS: ReadonlySet<string> = new Set<AiTool>([
  "devices_get",
  "devices_health",
  "health_alerts",
  "portal_my_tickets",
  "portal_my_ticket",
  "portal_categories",
  "portal_help_suggest",
  "portal_help_article",
  "actions_propose",
]);
const SOURCE_TYPES: ReadonlySet<string> = new Set<AiSourceType>([
  "ticket",
  "document",
  "device",
  "alert",
  "article",
  "category",
]);

export function ruleLabel(rule: string): string {
  if (RULES.has(rule)) return humaniseEnum(rule, "AiInjectionRule");
  const words = rule.replace(/[_-]+/g, " ").trim();
  return words === "" ? translate("aiAdmin.flags.unnamedRule") : humaniseStatus(rule);
}

/**
 * A tool as a person reads it. The wire name is `devices.health`; the
 * catalogue keys it `devices_health`. A tool the console has not met reads in
 * the shared status words, never as the wire name.
 */
export function toolLabel(tool: string): string {
  const key = tool.replace(/\./g, "_");
  return TOOLS.has(key) ? humaniseEnum(key, "AiTool") : humaniseStatus(key);
}

/** What kind of record a flag came from; the detector falls back to the tool's own name when a record names no type. */
export function sourceTypeLabel(type: string): string {
  if (SOURCE_TYPES.has(type)) return humaniseEnum(type, "AiSourceType");
  return toolLabel(type);
}

export function InjectionFlagList() {
  const t = useT();
  const flags = useAiInjectionFlags();
  const items = flags.data ?? [];

  return (
    <Panel
      title={t("aiAdmin.flags.title")}
      subtitle={t("aiAdmin.flags.subtitle")}
      bare
    >
      {flags.isError ? (
        // `flags.error.message` was rendered raw here. `ApiError` is built as
        // `new ApiError(status, await response.text())`, so a bodyless 503 —
        // which is what a proxy in front of this route returns — carried `""`
        // and this read "Flagged content is not available:" and then stopped.
        // Announced, because an administrator who cannot see the flagged
        // excerpts has to know the list is missing rather than empty.
        <p role="alert" className="notice red" style={{ margin: 14 }}>
          <Icon name={KIND_ICON.crit} size={16} />
          <span>{t("aiAdmin.flags.unavailable", { sentence: describeSubmitError(flags.error) })}</span>
        </p>
      ) : (
        <QueryState
          loading={flags.isPending}
          error={null}
          empty={items.length === 0}
          emptyLabel={t("aiAdmin.flags.empty")}
          emptyIcon="shield"
        >
          <Table>
            <thead>
              <tr>
                <Th>{t("aiAdmin.flags.record")}</Th>
                <Th>{t("aiAdmin.flags.rule")}</Th>
                <Th>{t("aiAdmin.flags.excerpt")}</Th>
              </tr>
            </thead>
            <tbody>
              {items.map((flag) => (
                <tr key={flag.id}>
                  <Td lead={<Icon name={KIND_ICON.warn} size={18} />} gutter="warn">
                    <div className="cell-title">{sourceTypeLabel(flag.source_type)}</div>
                    <div className="cell-sub">
                      <span className="mono">{flag.source_id}</span>
                      {" · "}
                      {t("aiAdmin.flags.foundWhile", { tool: toolLabel(flag.source_tool).toLocaleLowerCase() })}
                    </div>
                  </Td>
                  <Td>
                    <StatusPill value={flag.matched_rule} label={ruleLabel(flag.matched_rule)} kind="warn" />
                  </Td>
                  <Td>
                    <span
                      className="mono note"
                      style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}
                    >
                      {flag.excerpt}
                    </span>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </QueryState>
      )}
    </Panel>
  );
}
