// The org's AI settings screen (spec §3.4, §11.2, §0.14), for `ai.admin`.
//
// **THIS PAGE HAS NO FORM ANY MORE, AND THAT IS THE CHANGE.** It carried three
// controls — a model-provider picker, an egress acknowledgement tickbox and an
// "Enable the assistant for this organization" checkbox — and the operator's
// decision of 2026-08-25 removed all three at their source:
//
//   "dont allow anyone to choose an AI model, we will use the same for every
//    tenant and api key will be set in the .env per tenant, so this
//    functionality must be shipped enabled and working already."
//
// The provider, its model and its key are the deployment's `ITS_AI_*`
// environment; `ai_settings` has no `enabled` and no `provider_key` column
// (migration `0074`); `PUT /ai/settings` refuses all three fields. What is left
// for an org-admin to tune — retention, the two rate ceilings, the token budget
// — lives on Settings → Assistant, where it always did, so this page points
// there rather than growing a second copy of it.
//
// **THE EGRESS DISCLOSURE STAYS, VERBATIM, FROM ONE CONSTANT** (`egress.ts`).
// The tickbox beside it is gone; the obligation it sat next to is not. Tool
// results carry real customer data to the provider, an administrator is owed an
// accurate account of that, and that account does not depend on who ticked
// what. The consent moved to where the operator now makes it: writing `ITS_AI_*`
// into `.env` is deliberate, server-side and reviewable in configuration
// management, which a click never was.
//
// **THE PROVIDER IS SHOWN, NOT OFFERED.** `GET /ai/providers` serves key, kind
// and model — never a base URL, never key material — and an administrator
// reading "what leaves this network" is entitled to know where it goes. That is
// disclosure. Offering it back as a control would be the choice this decision
// removed.
//
// v3 draws no screen for this route, so it is composed from the kit alone: the
// head, a two-column `.g2` of "what leaves" beside the provider, and the
// flagged-content rows under both.

import { useT } from "../../i18n/context";
import { Icon } from "../../shell/icons";
import { KIND_ICON, PageHead, Panel } from "../../shell/components";
import { KV } from "../../shell/viz";
import { useAiProviders } from "./api";
import { EGRESS_DISCLOSURE } from "./egress";
import { InjectionFlagList } from "./InjectionFlagList";
import { describeProvider, providerOffSummary, serverAdminDetail, serverAdminHeading } from "./provider";

/**
 * A sub-heading inside a panel body, AS A CAPTION AND NOT AS A SECOND TITLE.
 *
 * It was `--t-body` 600 — 13.5px under a 15px panel title, a 1.5px step that
 * reads as no step at all, so "What never leaves" sat on the same rung as the
 * panel it is inside. `.k` is the kit's own caption (`--t-cap`, tracked,
 * uppercased in CSS, muted): three tokens of separation instead of half of
 * one, and the vocabulary v3 uses everywhere a group of lines needs naming
 * inside a panel. `.k` is one of the two classes the port brief exempts from
 * the no-ALL-CAPS rule, because the capitals are CSS and not the copy.
 */
const SUBHEAD_CLASS = "k";
const SUBHEAD = { marginTop: 14, marginBottom: 2 } as const;
/**
 * THE MARKERS ARE DECLARED HERE BECAUSE TAILWIND'S PREFLIGHT REMOVED THEM.
 * `list-style: none` on every `ul` is part of the reset, so these seven lines
 * rendered as sentences indented 18px by a `padding-left` with nothing at the
 * end of it — an indent that looked like a mistake rather than a list. The
 * disc goes back on, `outside` so the wrap of a long line aligns under the
 * first word and not under the marker.
 */
const LIST = {
  listStyle: "disc outside",
  paddingLeft: 18,
  margin: "6px 0 0",
} as const;
/** Block flow, not a grid: a grid item is not a `list-item` and loses its marker. */
const LIST_ITEM = { marginTop: 4 } as const;

export function AiSettingsPage() {
  const t = useT();
  const providers = useAiProviders();
  const provider = describeProvider(providers.data);

  return (
    <>
      <PageHead
        title={t("aiAdmin.settings.title")}
        description={t("aiAdmin.settings.description")}
      />
      <div className="g2" style={{ gridTemplateColumns: "1fr 1fr", marginBottom: 16 }}>
        <Panel title={EGRESS_DISCLOSURE.heading}>
          {/* Both lists are captioned, and the first one was not: four lines
              hanging straight off the panel title, then a heading, then three
              more, so the head that DID exist read as a divider rather than as
              one of a pair. */}
          <h3 className={SUBHEAD_CLASS} style={{ ...SUBHEAD, marginTop: 0 }}>
            {t("aiAdmin.settings.whatLeaves")}
          </h3>
          <ul className="note" style={LIST}>
            {EGRESS_DISCLOSURE.leaves.map((line) => (
              <li key={line} style={LIST_ITEM}>
                {line}
              </li>
            ))}
          </ul>
          <h3 className={SUBHEAD_CLASS} style={SUBHEAD}>
            {t("aiAdmin.settings.whatNeverLeaves")}
          </h3>
          <ul className="note" style={LIST}>
            {EGRESS_DISCLOSURE.neverLeaves.map((line) => (
              <li key={line} style={LIST_ITEM}>
                {line}
              </li>
            ))}
          </ul>
          <p className="note" style={{ marginTop: 12 }}>
            {EGRESS_DISCLOSURE.selfHosted}
          </p>
          <p className="note" style={{ marginTop: 4 }}>
            {EGRESS_DISCLOSURE.contractual}
          </p>
        </Panel>

        {/* THE PROVIDER, beside WHAT GOES. The two belong together: "tool
            results: real customer data" is only actionable beside the name of
            the thing receiving them. A `kv` list rather than a labelled field,
            because this is a fact being reported and not a control — a
            `<label>` here would promise an input that does not exist.

            TITLED "Provider", NOT "Where it goes" ANY MORE. Design review,
            2026-09-11: that exact phrase is also the glossary's own
            replacement for "channel" on Settings → Notifications — adjacent
            surfaces, one phrase, two referents. This panel already has a KV
            row labelled "Model provider" once a provider is configured; the
            heading now names the same thing rather than a phrase Notifications
            needs for something else. */}
        <Panel title={t("aiAdmin.settings.providerTitle")} subtitle={t("aiAdmin.settings.providerSubtitle")}>
          {providers.isLoading ? (
            <p className="note">{t("aiAdmin.settings.loading")}</p>
          ) : provider === null ? (
            <>
              <p role="status" className="notice gold">
                <Icon name={KIND_ICON.warn} size={16} />
                <span>{providerOffSummary()}</span>
              </p>
              {/* The variable names are for whoever can act on them, which is
                  never the reader of the sentence above -- see `provider.ts`'s
                  own comment on `PROVIDER_OFF_SUMMARY`. Same disclosure,
                  same wording, as `Settings → Assistant`'s `AssistantTab.tsx`. */}
              <details style={{ marginTop: 8 }}>
                <summary className="note cursor-pointer">{serverAdminHeading()}</summary>
                <p className="note" style={{ marginTop: 6 }}>{serverAdminDetail()}</p>
              </details>
            </>
          ) : (
            <>
              <KV
                items={[
                  [
                    t("aiAdmin.settings.modelProvider"),
                    <>
                      <b>{provider.key}</b>{" "}
                      <span className="note">{`· ${provider.detail}`}</span>
                    </>,
                  ],
                ]}
              />
              <p className="note" style={{ marginTop: 12 }}>
                {t("aiAdmin.settings.serverConfig")}
              </p>
            </>
          )}
          <p className="note" style={{ marginTop: 12 }}>
            {t("aiAdmin.settings.otherSettings")}
          </p>
        </Panel>
      </div>

      <InjectionFlagList />
    </>
  );
}
