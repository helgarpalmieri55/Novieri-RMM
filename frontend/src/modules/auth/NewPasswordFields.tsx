/**
 * A new password, twice (own-auth design §2.1): at least 12 characters (15
 * for a client's contact, `minimum`), at most 128, each code point counting
 * as one; any characters, spaces included; no composition rules, and pasting
 * from a password manager is welcome (NIST SP 800-63B-4 §3.1.1.2, [S1]). The
 * list of common and leaked passwords, and the words of the context, are the
 * server's to check: its `password_rejected` says which rule refused a
 * password, and this console shows that sentence
 * (`./errors.ts::passwordRefusal`).
 *
 * `helpKey`: the rule as a page that cannot know whose it is states it -- the
 * reset link names no account, so `/reset-password` says both minimums
 * (final review F, item 1). A key, not text: `i18n/coverage.test.ts` reads a
 * `help` literal as English, the reason `MicrosoftButton` takes `labelKey`.
 */
import { Field, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";

export function NewPasswordFields({
  password,
  again,
  minimum = 12,
  helpKey = "auth.password.rules",
  onPassword,
  onAgain,
}: {
  password: string;
  again: string;
  /** The shortest password allowed: 12 for a technician, 15 for a client's contact. */
  minimum?: number;
  /** The rule's sentence; `{min}` in it is `minimum`. */
  helpKey?: MessageKey;
  onPassword: (value: string) => void;
  onAgain: (value: string) => void;
}) {
  const t = useT();
  return (
    <>
      <Field label={t("auth.password.new")} help={t(helpKey, { min: minimum })}>
        <input
          className={inputClass}
          type="password"
          autoComplete="new-password"
          value={password}
          onChange={(event) => onPassword(event.target.value)}
        />
      </Field>
      <Field label={t("auth.password.confirm")}>
        <input
          className={inputClass}
          type="password"
          autoComplete="new-password"
          value={again}
          onChange={(event) => onAgain(event.target.value)}
        />
      </Field>
    </>
  );
}
