/**
 * THE SITE A NEW ROW GOES TO (phase 3 design §3.2): an enrollment token, an
 * install link or command, a hand-made asset, a managed deployment of the
 * agent. Its client's live sites; with one, it is already chosen. The
 * internal client's "Principal" is no longer the only site there is.
 *
 * Nothing is listed before the client is known -- a form takes it from its
 * parent, from the top bar, or from its Client field
 * (`lib/clientSelection.ts`) -- and a site of another client is never kept:
 * when the client changes, the choice goes.
 *
 * INVARIANT 6: THIS GATES NOTHING. The server refuses a site outside the
 * request's scope whatever this offered.
 */

import { useEffect } from "react";

import { useT } from "../i18n/context";
import { useClientSites } from "../lib/clientSites";
import { Field, inputClass } from "./ui";

export function SiteField({
  clientId,
  value,
  onChange,
}: {
  /** The row's client, or `null` while it is not known. */
  clientId: string | null;
  /** The site chosen, or `""`. */
  value: string;
  onChange: (siteId: string) => void;
}) {
  const t = useT();
  const sites = useClientSites(clientId);
  const live = (sites.data ?? []).filter((site) => site.archived_at === null);
  const only = live.length === 1 ? (live[0]?.id ?? null) : null;
  const known = sites.data !== undefined;
  const kept = value === "" || live.some((site) => site.id === value);

  useEffect(() => {
    if (known && !kept) onChange("");
    else if (value === "" && only !== null) onChange(only);
  }, [known, kept, value, only, onChange]);

  // LOADING IS SAID BY THE EMPTY OPTION, NOT BY THE HELP. `Field` wraps its
  // control only while it has help, so a help shown while the sites load and
  // dropped once they arrive would remount the <select> under the person.
  const help =
    clientId === null
      ? t("components.siteField.chooseClientFirst")
      : sites.isLoading
        ? undefined
        : sites.isError
          ? t("components.siteField.failed")
          : live.length === 0
            ? t("components.siteField.none")
            : undefined;

  return (
    <Field label={t("components.siteField.label")} help={help}>
      <select
        className={inputClass}
        required
        value={kept ? value : ""}
        disabled={live.length === 0}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">
          {clientId !== null && sites.isLoading
            ? t("components.siteField.loading")
            : t("components.siteField.choose")}
        </option>
        {live.map((site) => (
          <option key={site.id} value={site.id}>
            {site.name}
          </option>
        ))}
      </select>
    </Field>
  );
}
