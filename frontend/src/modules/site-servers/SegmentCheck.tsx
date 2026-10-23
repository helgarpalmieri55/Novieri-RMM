// Declared segments versus what inventory actually saw.
//
// THE THREE STATES BELOW ARE THREE DIFFERENT OPERATIONAL FACTS AND MUST NEVER
// RENDER AS EACH OTHER:
//
//   * `evidence: "unavailable"` -- IPAM is not installed and NOTHING WAS LOOKED
//     UP. The three lists are empty for a reason that has nothing to do with
//     the declaration. Rendering this as "no mismatches" would tell an operator
//     their configuration is verified when it has never been checked. It is the
//     single most consequential rendering decision in this module.
//   * `declared_unobserved` -- declared, and inventory has not placed the
//     machine there. NOT an error: promotion deliberately accepts an operator's
//     claim without verifying it, and a freshly promoted branch server has no
//     observation until inventory next runs. It DOES mean the server has no
//     sweep preference on that segment yet, and saying so is the useful part.
//   * `observed_undeclared` -- an address on this device inside no declared
//     segment. Usually a second NIC; sometimes a declaration that is wrong.
//
// ON THE v3 KIT: the unverifiable case is a warn notice (the glyph carries it,
// not the colour alone); the checked case is a `.kv` list, one row per
// question, each answer a mono list or the sentence that says why it is empty.

import { useT } from "../../i18n/context";
import { Icon } from "../../shell/icons";
import type { SegmentCheckOut } from "./types";

const LIST_STYLE = { margin: 0, paddingLeft: 18 } as const;

export function SegmentCheck({
  check,
  isLoading,
}: {
  check: SegmentCheckOut | undefined;
  isLoading?: boolean;
}) {
  const t = useT();
  if (isLoading) return <p className="note">{t("siteServers.check.checking")}</p>;
  if (!check) return null;

  if (check.evidence === "unavailable") {
    return (
      <section aria-label={t("siteServers.check.region")}>
        <div className="notice gold">
          <Icon name="alert-triangle" size={16} />
          <p data-testid="segment-check-evidence" role="status">
            {t("siteServers.check.unavailable")}
          </p>
        </div>
      </section>
    );
  }

  return (
    <section aria-label={t("siteServers.check.region")}>
      <p className="note" data-testid="segment-check-evidence" style={{ marginBottom: 12 }}>
        {t("siteServers.check.compared")}
      </p>

      <dl className="kv">
        <dt>{t("siteServers.check.matched")}</dt>
        <dd>
          {check.matched.length === 0 ? (
            <span data-testid="matched-empty">{t("siteServers.check.matchedEmpty")}</span>
          ) : (
            <ul data-testid="matched" className="mono" style={LIST_STYLE}>
              {check.matched.map((segment) => (
                <li key={segment}>{segment}</li>
              ))}
            </ul>
          )}
        </dd>

        <dt>{t("siteServers.check.declaredUnobserved")}</dt>
        <dd>
          {check.declared_unobserved.length === 0 ? (
            <span data-testid="declared-unobserved-empty">
              {t("siteServers.check.declaredUnobservedEmpty")}
            </span>
          ) : (
            /* THE VALUE SAID THE LABEL BACK. Each row read "10.20.9.0/24:
               declared, not yet observed. No sweep preference here until
               inventory places this machine on the segment." -- the first
               clause is the `<dt>` above it, repeated once per segment, and
               the consequence was repeated with it. The prefixes are the
               value; the consequence is true of all of them, so it is said
               once under the list. */
            <>
              <ul data-testid="declared-unobserved" className="mono" style={LIST_STYLE}>
                {check.declared_unobserved.map((segment) => (
                  <li key={segment}>{segment}</li>
                ))}
              </ul>
              <p className="note" style={{ marginTop: 4 }}>
                {t("siteServers.check.noPreference", { count: check.declared_unobserved.length })}
              </p>
            </>
          )}
        </dd>

        <dt>{t("siteServers.check.observedUndeclared")}</dt>
        <dd>
          {check.observed_undeclared.length === 0 ? (
            <span data-testid="observed-undeclared-empty">
              {t("siteServers.check.observedUndeclaredEmpty")}
            </span>
          ) : (
            <ul data-testid="observed-undeclared" className="mono" style={LIST_STYLE}>
              {check.observed_undeclared.map((address) => (
                <li key={address}>{address}</li>
              ))}
            </ul>
          )}
        </dd>
      </dl>
    </section>
  );
}
