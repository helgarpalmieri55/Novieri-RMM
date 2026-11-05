/**
 * WHAT A TAB SHOWS ONCE ITS SESSION HAS ENDED (operator decision 3,
 * 2026-11-05, "Ocultar y vaciar"), in place of the shell and the page.
 *
 * NOTHING OF THE TENANT'S IS DRAWN HERE: no rail, no top bar, no client
 * selector, no organisation, no name -- the guard has emptied the query cache
 * these came from (`App.tsx`'s `RequireAuth`). An idle timeout on a shared
 * computer ends the server's session, and whoever sits down next must not
 * find the last page still on screen (final review D1, Minor 1).
 *
 * WHAT IS KEPT IS WHAT THE PERSON TYPED, read-only, so they can copy it
 * before signing in again (`lib/unsavedText.ts`): never a password, never any
 * other secret. It lives in the guard's state only and is gone with the
 * document.
 *
 * NO NAVIGATION OF ITS OWN (plan decision D20). "Sign in again" is the one way
 * out, and it is the person's: a redirect would throw away the text this
 * screen exists to keep.
 *
 * On the sign-in pages' own card (`modules/auth/AuthCard.tsx`): outside the
 * shell, one `<main>` with one `<h1>`, and a language switch -- which saves
 * nothing to an account here, because there is no session to save it with.
 */
import { useT } from "../i18n/context";
import { AuthCard, AuthSection } from "../modules/auth/AuthCard";
import { Field, inputClass } from "../components/ui";
import type { KeptText } from "../lib/unsavedText";
import { Btn } from "./components";

export function SessionEnded({ kept, onSignIn }: { kept: readonly KeptText[]; onSignIn: () => void }) {
  const t = useT();
  return (
    <AuthCard title={t("shell.sessionEnded.title")}>
      <p role="status" className="note" style={{ margin: 0 }}>
        {t(kept.length > 0 ? "shell.sessionEnded.bodyKept" : "shell.sessionEnded.body")}
      </p>
      <div>
        <Btn variant="primary" icon="arrow-right" label={t("shell.app.signInAgain")} onClick={onSignIn} />
      </div>
      {kept.length > 0 ? (
        // A named section (`AuthSection`), so it is the region "What you were typing".
        <AuthSection title={t("shell.sessionEnded.keptTitle")} hint={t("shell.sessionEnded.keptHint")}>
          {kept.map((field, index) => (
            <Field key={index} label={field.label || t("shell.sessionEnded.keptField", { n: index + 1 })}>
              <textarea
                className={inputClass}
                readOnly
                value={field.text}
                rows={Math.min(8, Math.max(2, field.text.split("\n").length))}
              />
            </Field>
          ))}
        </AuthSection>
      ) : null}
    </AuthCard>
  );
}
