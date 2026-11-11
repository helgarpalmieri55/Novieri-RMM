/**
 * A ONE-TIME LINK, SHOWN ONCE: the invitation link the server hands back
 * when no mail carried it -- a technician's (Settings → Users) or a client's
 * contact's (a client's Contacts tab; own-auth phase 2, Task 11, fix round
 * 1). The server keeps only the link's hash, so this panel holds the one
 * copy there is. It stays until the person says they passed it on.
 *
 * A COPY THAT FAILED SAYS SO. A person who pressed "Copy", saw nothing and
 * pressed "Done" would lose the link, so the copy answers "Copied." or that
 * the browser would not copy it, and the box selects itself for copying by
 * hand.
 *
 * `data-secret`: the box is read-only, and the one value that opens an
 * account (`lib/unsavedText.ts` never keeps what is inside one). The words
 * are the caller's, from its own catalogue (`text`): `title` and `once` name
 * the person as `{email}`.
 *
 * A NEW LINK STARTS AFRESH: the panel is keyed by the link, so "Copied." about
 * an earlier one is never shown beside a link nobody copied.
 */
import { useState } from "react";

import { useT } from "../i18n/context";
import type { MessageKey } from "../i18n/core";
import { FOLDING_FORM_GRID } from "../modules/settings/formGrid";
import { Btn, KIND_ICON, Panel } from "../shell/components";
import { Icon } from "../shell/icons";
import { Field, inputClass } from "./ui";

/** The caller's catalogue keys for the panel's words. */
export interface ShownOnceLinkText {
  /** The panel's title and the region's name, with `{email}`. */
  title: MessageKey;
  /** Why the link is shown, and that it is shown only now, with `{email}`. */
  once: MessageKey;
  label: MessageKey;
  copy: MessageKey;
  copied: MessageKey;
  copyFailed: MessageKey;
  done: MessageKey;
}

export interface ShownOnceLinkProps {
  /** Whom the link is for. */
  email: string;
  link: string;
  text: ShownOnceLinkText;
  /** "Done": the caller forgets the link. */
  onDone: () => void;
}

/** Copying without swallowing the failure (`enrollment/EnrollmentPage.tsx`'s pattern). */
async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export function ShownOnceLink(props: ShownOnceLinkProps) {
  return <ShownOnceLinkPanel key={props.link} {...props} />;
}

function ShownOnceLinkPanel({ email, link, text, onDone }: ShownOnceLinkProps) {
  const t = useT();
  const [copied, setCopied] = useState<"copied" | "failed" | null>(null);
  const title = t(text.title, { email });

  return (
    // SHOWN ONCE: the server keeps only the link's hash, so this is the one
    // copy there is. It stays until the person says it was passed on.
    <section aria-label={title}>
      <Panel title={title}>
        <div className="notice gold" role="status">
          <Icon name={KIND_ICON.warn} size={16} />
          <span>{t(text.once, { email })}</span>
        </div>
        <div className="form-grid" style={{ ...FOLDING_FORM_GRID, marginTop: 12 }}>
          <Field label={t(text.label)}>
            <input
              className={inputClass}
              data-secret
              readOnly
              value={link}
              onFocus={(event) => event.currentTarget.select()}
            />
          </Field>
        </div>
        <div className="form-actions">
          <Btn
            icon="copy"
            label={t(text.copy)}
            onClick={() => void copyToClipboard(link).then((ok) => setCopied(ok ? "copied" : "failed"))}
          />
          <Btn variant="primary" icon="check" label={t(text.done)} onClick={onDone} />
          {copied === "copied" ? (
            <span className="form-saved">
              <Icon name={KIND_ICON.ok} size={13} />
              {t(text.copied)}
            </span>
          ) : null}
          {copied === "failed" ? (
            <span role="alert" className="form-error">
              <Icon name={KIND_ICON.crit} size={13} />
              {t(text.copyFailed)}
            </span>
          ) : null}
        </div>
      </Panel>
    </section>
  );
}
