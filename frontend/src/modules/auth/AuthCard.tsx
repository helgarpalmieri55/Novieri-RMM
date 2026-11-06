/**
 * The ground every page reached without a session stands on (own-auth Task
 * 11): one card on the page ground, the page-title rung for its headline,
 * and a language switch -- the same composition as the first-boot screen
 * (`modules/setup/SetupPage.tsx`), because v3 draws no sign-in screen and
 * these, like that one, are built from the kit alone.
 *
 * OUTSIDE THE SHELL, AND IT MUST BE: the shell asks `/me` for a session these
 * pages exist to create. So the card is the page's `<main>` landmark, with
 * its one `<h1>`, and every colour and size is a token (the harmony gate
 * walks these routes).
 *
 * THE LANGUAGE SWITCH IS HERE because nothing else on these pages can change
 * it, and a person who chose Spanish on another computer has no saved
 * language this browser knows of yet. The choice is kept in this browser
 * (`itms.lang`); it is not saved to an account, because there is no session
 * to save it with (`App.tsx`'s `LanguageBridge`, plan decision D20).
 */
import { useId, type CSSProperties, type ReactNode } from "react";

import { useLanguage, useT } from "../../i18n/context";
import { LANGUAGES } from "../../i18n/types";
import { Btn, KIND_ICON } from "../../shell/components";
import { Icon } from "../../shell/icons";

const GROUND: CSSProperties = {
  minHeight: "100vh",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  background: "var(--bg)",
  padding: 16,
};
const EMBEDDED_GROUND: CSSProperties = { ...GROUND, minHeight: 0 };
const CARD: CSSProperties = { width: "100%", maxWidth: 480, padding: 32, margin: 0 };
/** The page-title rung (`.ph h1`), on a screen that has no `.ph`. */
const HEADLINE: CSSProperties = { fontSize: "var(--t-h1)", fontWeight: 600, letterSpacing: "-.015em", margin: 0 };
/** The panel-title rung (`.panel-h h2`), for one choice inside the card. */
const SECTION_HEADING: CSSProperties = { fontSize: "var(--t-h2)", fontWeight: 600, margin: 0 };

/**
 * `embedded`: the same card drawn inside another page -- `/__preview`, where
 * the browser's a11y pass measures it -- as a region named by its headline,
 * not a second `<main>` (which axe refuses), and without the full-height ground.
 */
export function AuthCard({
  title,
  intro,
  children,
  embedded = false,
}: {
  title: string;
  intro?: ReactNode;
  children: ReactNode;
  embedded?: boolean;
}) {
  const t = useT();
  const { language, setLanguage } = useLanguage();
  const headingId = useId();
  const Ground = embedded ? "section" : "main";
  return (
    <Ground style={embedded ? EMBEDDED_GROUND : GROUND} aria-labelledby={embedded ? headingId : undefined}>
      <div className="panel" style={CARD}>
        <h1 id={headingId} style={HEADLINE}>
          {title}
        </h1>
        {intro ? (
          <p className="note" style={{ marginTop: 6 }}>
            {intro}
          </p>
        ) : null}
        <div style={{ marginTop: 20, display: "grid", gap: 16 }}>{children}</div>
        <div role="group" aria-label={t("common.language.label")} style={{ marginTop: 24, display: "flex", gap: 8 }}>
          {LANGUAGES.map((option) => (
            <Btn
              key={option}
              small
              variant="ghost"
              label={option === "es" ? t("common.language.spanish") : t("common.language.english")}
              pressed={language === option}
              onClick={() => setLanguage(option)}
            />
          ))}
        </div>
      </div>
    </Ground>
  );
}

/** A refusal or a failure, announced: these pages change under a person who just pressed a button. */
export function AuthAlert({ text }: { text: string | null }) {
  if (!text) return null;
  return (
    <p role="alert" className="notice red">
      <Icon name={KIND_ICON.crit} size={16} />
      <span>{text}</span>
    </p>
  );
}

/** Something that happened as asked, said once, politely. */
export function AuthDone({ children }: { children: ReactNode }) {
  return (
    <p role="status" className="notice">
      <Icon name={KIND_ICON.ok} size={16} />
      <span>{children}</span>
    </p>
  );
}

/** One way through a step (a passkey, an app, a recovery code), named by its own heading. */
export function AuthSection({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  const id = useId();
  return (
    <section aria-labelledby={id} style={{ display: "grid", gap: 10 }}>
      <h2 id={id} style={SECTION_HEADING}>
        {title}
      </h2>
      {hint ? (
        <p className="note" style={{ margin: 0 }}>
          {hint}
        </p>
      ) : null}
      {children}
    </section>
  );
}
