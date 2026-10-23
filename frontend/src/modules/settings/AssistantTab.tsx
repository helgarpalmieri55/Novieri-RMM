// Assistant — `GET /ai/settings`, `GET /ai/providers`, `PUT /ai/settings`,
// all three gated `ai.admin`.
//
// THERE IS NO API-KEY FIELD AND NO BASE-URL FIELD, AND THAT IS NOT AN
// OVERSIGHT (invariant 3, and the AI module's spec §0.10). Provider key
// material is server configuration (`ITS_AI_*`); it is never returned by
// `GET /ai/providers` — which serves key, kind and model only — so there is
// nothing to display and nothing to pre-fill. A base-URL box would be an SSRF
// primitive the platform deliberately does not have.
//
// AND SINCE §0.14 THERE IS NO PROVIDER PICKER AND NO ENABLE SWITCH EITHER.
// The operator's decision of 2026-08-25 — "dont allow anyone to choose an AI
// model, we will use the same for every tenant and api key will be set in the
// .env per tenant, so this functionality must be shipped enabled and working
// already" — makes the deployment's environment the one provider and the one
// switch. `ai_settings` has no `enabled` and no `provider_key` column
// (migration `0074`), so this tab writes RATE AND RETENTION ONLY: cost and
// retention are per-org, reach is not. The provider is shown, because an
// administrator reading what leaves is entitled to know where it goes.
//
// THE EGRESS DISCLOSURE IS IMPORTED, NOT RETYPED. `../ai-admin/egress` is the
// single constant the assistant's own settings page renders, so what an
// operator is shown here cannot drift from what that page shows or from what
// the spec says leaves the network. The no-provider warning (`PROVIDER_OFF_
// SUMMARY`, `SERVER_ADMIN_HEADING`, `SERVER_ADMIN_DETAIL`) is imported from
// `../ai-admin/provider` for the same reason: those three shipped as
// text-identical local copies in this file and in `AiSettingsPage.tsx` before
// this comment was written, which is two copies waiting to disagree, not two.
//
// THE ACKNOWLEDGEMENT CHECKBOX IS GONE, NOT MOVED HERE. It gated an enable that
// no longer exists, and a required attestation that can never be refused is a
// ceremony rather than a control. The consent is now the act of writing
// `ITS_AI_*` into `.env` — server-side, by the operator, and reviewable.
// Invariant 6 is untouched: the frontend still gates nothing.

import { useEffect, useState } from "react";

import { Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { FieldGroup, KIND_ICON, Panel } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { KV } from "../../shell/viz";
import { EGRESS_DISCLOSURE } from "../ai-admin/egress";
import {
  describeProvider,
  providerOffSummary,
  serverAdminDetail,
  serverAdminHeading,
} from "../ai-admin/provider";
import type { AiSettingsOut, AiSettingsUpdate } from "../ai-admin/types";
import { useAiProviders, useAiSettings, useUpdateAiSettingsMutation } from "./api";
import { FOLDING_FORM_GRID } from "./formGrid";
import { LoadFailure } from "./LoadFailure";
import { SaveBar } from "./SaveBar";
import { isDirty, isForbidden, parseIntegerField, permissionReason } from "./settingsState";

export const AI_ADMIN = "ai.admin";


/** The disclosure's own head: the body rung at 600, under the panel's `h2`. */
const SUBHEAD = { fontSize: "var(--t-body)", fontWeight: 600, margin: "14px 0 4px" } as const;
/**
 * The head of ONE LIST inside that section, as a caption.
 *
 * "What never leaves" was a second `--t-body` 600 head under the first one, so
 * the two rungs of this panel body were the same rung. `.k` is the kit's
 * caption (`--t-cap`, tracked, uppercased in CSS, muted) — three tokens below
 * the head it now sits under instead of zero. The same change is made on
 * `/assistant/settings`, which renders this disclosure from the same constant.
 */
const LIST_HEAD = { marginTop: 12, marginBottom: 2 } as const;
/**
 * THE MARKERS ARE DECLARED BECAUSE TAILWIND'S PREFLIGHT REMOVED THEM: without
 * `list-style` these lines were sentences indented by a `padding-left` with
 * nothing at the end of it. Block flow rather than a grid, because a grid item
 * is not a `list-item` and loses its marker again.
 */
const LIST = { listStyle: "disc outside", paddingLeft: 18, margin: 0 } as const;
const LIST_ITEM = { marginTop: 4 } as const;

interface AssistantForm {
  transcriptRetentionDays: string;
  maxTurnsPerUserPerMinute: string;
  maxTurnsPerOrgPerHour: string;
  dailyTokenBudget: string;
}

function seedFrom(data: AiSettingsOut | undefined): AssistantForm {
  return {
    transcriptRetentionDays: data ? String(data.transcript_retention_days) : "",
    maxTurnsPerUserPerMinute: data ? String(data.max_turns_per_user_per_minute) : "",
    maxTurnsPerOrgPerHour: data ? String(data.max_turns_per_org_per_hour) : "",
    // `null` is "no budget", which is a blank box, not a zero.
    dailyTokenBudget: data?.daily_token_budget == null ? "" : String(data.daily_token_budget),
  };
}

export function AssistantTab({ canAdmin }: { canAdmin: boolean }) {
  const t = useT();
  const settings = useAiSettings(canAdmin);
  const providers = useAiProviders(canAdmin);
  const update = useUpdateAiSettingsMutation();

  const provider = describeProvider(providers.data);
  const seed = seedFrom(settings.data);
  const [form, setForm] = useState<AssistantForm>(seed);

  const seedSignature = JSON.stringify(seed);
  useEffect(() => {
    setForm(JSON.parse(seedSignature) as AssistantForm);
  }, [seedSignature]);

  const forbidden = isForbidden(settings.error) || isForbidden(providers.error);
  const failed = (settings.error != null || providers.error != null) && !forbidden;
  const dirty = isDirty(form, seed);
  const editable = canAdmin && settings.data != null;

  function submit() {
    const body: AiSettingsUpdate = {};
    const retention = parseIntegerField(form.transcriptRetentionDays);
    if (retention != null) body.transcript_retention_days = retention;
    const perUser = parseIntegerField(form.maxTurnsPerUserPerMinute);
    if (perUser != null) body.max_turns_per_user_per_minute = perUser;
    const perOrg = parseIntegerField(form.maxTurnsPerOrgPerHour);
    if (perOrg != null) body.max_turns_per_org_per_hour = perOrg;
    // A cleared budget box is an explicit "no budget" (null), not a dropped field.
    body.daily_token_budget = parseIntegerField(form.dailyTokenBudget);
    update.mutate(body);
  }

  return (
    <Panel
      title={t("settings.assistant.title")}
      subtitle={t("settings.assistant.subtitle")}
    >
      {settings.isLoading || providers.isLoading ? (
        <p className="note">{t("settings.assistant.loading")}</p>
      ) : forbidden ? (
        <p className="note">
          {t("settings.assistant.forbidden", { code: AI_ADMIN })}
        </p>
      ) : failed ? (
        <LoadFailure error={settings.error ?? providers.error} />
      ) : (
        <>
          {/* THE SUMMARY IS ABOVE THE FOLD; THE ELEVEN BULLETS ARE NOT GONE,
              THEY ARE BEHIND A DISCLOSURE (language pass, 2026-09-11). The
              content is security-critical and stays verbatim -- only its
              weight changes: a reader meets one sentence, then the controls,
              and opens the disclosure for the full account this platform owes
              an administrator of what leaves their network. Reuses the h3/h4
              structure this file already had for the list rather than
              inventing a second idiom for it -- only now inside a native
              `<details>`, which is keyboard-operable and exposes its own
              expanded state for free. */}
          {/* The one-line version of `EGRESS_DISCLOSURE`, for above the fold. The full,
              security-critical list stays verbatim in the disclosure below; this is
              only what sits ABOVE it, so the controls are what a reader meets first. */}
          <p className="note" style={{ marginTop: 0 }}>{t("settings.assistant.egressSummary")}</p>
          <details style={{ marginTop: 8 }}>
            <summary className="note cursor-pointer">
              {t("settings.assistant.egressFull")}
            </summary>
            <h3 style={SUBHEAD}>{EGRESS_DISCLOSURE.heading}</h3>
            <h4 className="k" style={{ ...LIST_HEAD, marginTop: 6 }}>
              {t("settings.assistant.whatLeaves")}
            </h4>
            <ul className="note" style={LIST}>
              {EGRESS_DISCLOSURE.leaves.map((line) => (
                <li key={line} style={LIST_ITEM}>
                  {line}
                </li>
              ))}
            </ul>
            <h4 className="k" style={LIST_HEAD}>
              {t("settings.assistant.whatNeverLeaves")}
            </h4>
            <ul className="note" style={LIST}>
              {EGRESS_DISCLOSURE.neverLeaves.map((line) => (
                <li key={line} style={LIST_ITEM}>
                  {line}
                </li>
              ))}
            </ul>
            <p className="note" style={{ marginTop: 12 }}>{EGRESS_DISCLOSURE.selfHosted}</p>
            <p className="note" style={{ marginTop: 4 }}>{EGRESS_DISCLOSURE.contractual}</p>
          </details>

          {/* THE PROVIDER — a STATEMENT, not a control (§0.14). This was an
              `EntityPicker` over `GET /ai/providers`; the route still feeds it
              and the value it feeds is now read-only, because the deployment's
              `ITS_AI_*` names one provider for every organization on it. A
              `kv` list rather than a labelled field: a `<label>` promises an
              input, and there is none.

              The unconfigured case keeps the audit's fix ([HIGH],
              /assistant/settings → "Model provider": "the assistant cannot be
              enabled and the UI never says why"). Same dead end, same shared
              constant, so the two screens cannot explain it differently — only
              the consequence sentence changed, because there is no longer an
              enable for it to block.

              HEADED "Provider", NOT "Where it goes" ANY MORE. Design review,
              2026-09-11: that exact phrase is the glossary's own replacement
              for "channel" on Settings → Notifications, a tab one click away
              — same phrase, two referents. The KV row two lines down is
              already labelled "Model provider"; the heading now names the
              same thing instead of colliding with what Notifications needs
              the phrase for. */}
          <h3 style={SUBHEAD}>{t("settings.assistant.provider")}</h3>
          {provider === null ? (
            <>
              <p role="status" className="notice gold">
                <Icon name={KIND_ICON.warn} size={16} />
                <span>{providerOffSummary()}</span>
              </p>
              {/* The variable names are for whoever can act on them, which is
                  never the reader of the sentence above -- see `provider.ts`'s
                  own comment on `PROVIDER_OFF_SUMMARY`. Same disclosure, same
                  constant, as `/assistant/settings`. */}
              <details style={{ marginTop: 8 }}>
                <summary className="note cursor-pointer">{serverAdminHeading()}</summary>
                <p className="note" style={{ marginTop: 6 }}>{serverAdminDetail()}</p>
              </details>
            </>
          ) : (
            <KV
              items={[
                [
                  t("settings.assistant.modelProvider"),
                  <>
                    {/* "Local · openai-compatible · gpt-oss-120b", the way the
                        design writes it and the way /assistant/settings does.
                        Two surfaces, one line, one shape. */}
                    <b>{provider.key}</b>{" "}
                    <span className="note">{`· ${provider.detail}`}</span>
                  </>,
                ],
              ]}
            />
          )}

          {/* Retention answers "how long is it kept"; the ceilings answer "how
              much can be spent". One grid asked both at once. The wrapper keeps
              the 16px the grid used to carry away from the disclosure above. */}
          <div style={{ marginTop: 16 }}>
            <FieldGroup title={t("settings.assistant.groups.retention")} icon="history">
              <div className="form-grid" style={FOLDING_FORM_GRID}>
                <Field label={t("settings.assistant.keepFor")} help={t("settings.assistant.days")}>
                  <input
                    className={inputClass}
                    type="number"
                    min={1}
                    value={form.transcriptRetentionDays}
                    disabled={!editable}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        transcriptRetentionDays: event.target.value,
                      }))
                    }
                  />
                </Field>
              </div>
            </FieldGroup>
            <FieldGroup title={t("settings.assistant.groups.rateLimits")} icon="clock">
              <div className="form-grid" style={FOLDING_FORM_GRID}>
                <Field label={t("settings.assistant.perUser")}>
                  <input
                    className={inputClass}
                    type="number"
                    min={1}
                    value={form.maxTurnsPerUserPerMinute}
                    disabled={!editable}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        maxTurnsPerUserPerMinute: event.target.value,
                      }))
                    }
                  />
                </Field>
                <Field label={t("settings.assistant.perOrg")}>
                  <input
                    className={inputClass}
                    type="number"
                    min={1}
                    value={form.maxTurnsPerOrgPerHour}
                    disabled={!editable}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        maxTurnsPerOrgPerHour: event.target.value,
                      }))
                    }
                  />
                </Field>
                <Field label={t("settings.assistant.dailyLimit")} help={t("settings.assistant.noLimit")}>
                  <input
                    className={inputClass}
                    type="number"
                    min={0}
                    value={form.dailyTokenBudget}
                    disabled={!editable}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        dailyTokenBudget: event.target.value,
                      }))
                    }
                  />
                </Field>
              </div>
            </FieldGroup>
          </div>

          <p className="note" style={{ marginTop: 12 }}>
            {t("settings.assistant.serverNote")}
          </p>

          <SaveBar
            label={t("settings.assistant.save")}
            canEdit={editable}
            disabledReason={permissionReason(AI_ADMIN)}
            dirty={dirty}
            pending={update.isPending}
            error={update.error}
            saved={update.isSuccess && !dirty}
            onSave={submit}
          />
        </>
      )}
    </Panel>
  );
}
