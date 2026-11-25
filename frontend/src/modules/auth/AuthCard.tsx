/**
 * The ground every page reached without a session stands on (own-auth Task
 * 11; redesign design §4, plan Task 13): the Novieri RMM lockup, one card on
 * the page ground with the page-title rung for its headline, and below the
 * card the language switch and the product's name. The first-boot screen
 * (`modules/setup/SetupPage.tsx`) and the consent page stand on it too.
 *
 * THE BRAND IS ABOVE THE CARD, NOT IN IT (redesign design §4: «las pantallas
 * de acceso y el consentimiento OAuth reciben la misma marca dentro de su
 * propio marco»; DESIGN.md "Brand": the lockup is for the sign-in screens).
 * The card stays what it was -- 10px, the one shadow, 32px of padding -- and
 * holds only the step. The foot says the product's name and nothing else
 * (PRODUCT.md, "Evidence on Hand": no tagline, no claim).
 *
 * THE TAB IS NAMED AFTER THE HEADLINE: "<headline> · Novieri RMM" (redesign
 * design §5, WCAG 2.4.2). Until the redesign every one of these screens was
 * called what `index.html` is called. Written here, from the headline the
 * page already passes, so a page cannot name its tab one thing and its card
 * another; and only when the card IS the page (`embedded` leaves the tab to
 * the page that draws it). A headline never carries a link's token: the
 * pages read it from the fragment and keep it out of everything they draw.
 *
 * OUTSIDE THE SHELL, AND IT MUST BE: the shell asks `/me` for a session these
 * pages exist to create. So the frame is the page's `<main>` landmark, with
 * its one `<h1>`, and every colour and size is a token (the harmony gate
 * walks these routes). The styles stay inline, on tokens, and do not move to
 * a stylesheet of this module (plan Task 13, pre-flight W-M3):
 * `jsx-tokens.test.ts` guards them where they are, and the CSS guards read
 * only `src/shell/*.css` and `index.css`.
 *
 * THE LANGUAGE SWITCH IS HERE because nothing else on these pages can change
 * it, and a person who chose Spanish on another computer has no saved
 * language this browser knows of yet. The choice is kept in this browser
 * (`itms.lang`); it is not saved to an account, because there is no session
 * to save it with (`App.tsx`'s `LanguageBridge`, plan decision D20).
 */
import { useId, type CSSProperties, type MouseEvent, type ReactNode } from "react";

import { useLanguage, useT } from "../../i18n/context";
import { LANGUAGES } from "../../i18n/types";
import { NovieriLockup, PRODUCT_NAME } from "../../shell/brand";
import { Btn, KIND_ICON } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useDocumentTitle } from "../../shell/title";

const GROUND: CSSProperties = {
  minHeight: "100vh",
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  justifyContent: "center",
  background: "var(--bg)",
  color: "var(--ink)",
  padding: "32px 16px",
};
const EMBEDDED_GROUND: CSSProperties = { ...GROUND, minHeight: 0 };
/** The card's measure, which the lockup above it and the foot below it share. */
const MEASURE = 480;
const BRAND: CSSProperties = { width: "100%", maxWidth: MEASURE, display: "flex", justifyContent: "center", marginBottom: 24 };
const CARD: CSSProperties = { width: "100%", maxWidth: MEASURE, padding: 32, margin: 0 };
const FOOT: CSSProperties = {
  width: "100%",
  maxWidth: MEASURE,
  marginTop: 16,
  display: "flex",
  flexWrap: "wrap",
  alignItems: "center",
  justifyContent: "space-between",
  gap: 8,
};
/** The page-title rung (`.ph h1`), on a screen that has no `.ph`. */
const HEADLINE: CSSProperties = { fontSize: "var(--t-h1)", fontWeight: 600, letterSpacing: "-.015em", margin: 0 };
/** The panel-title rung (`.panel-h h2`), for one choice inside the card. */
const SECTION_HEADING: CSSProperties = { fontSize: "var(--t-h2)", fontWeight: 600, margin: 0 };
/** "or" between two ways through, on a hairline each side. */
const OR: CSSProperties = { display: "flex", alignItems: "center", gap: 12, margin: 0 };
const OR_RULE: CSSProperties = { flex: "1 1 auto", borderTop: "1px solid var(--line)" };
/**
 * A step's action at the card's full width. `.btn` is `inline-flex` with its
 * content at the start, so a stretched one needs its content centred, and
 * `Btn` takes no style: this is the kit's button, drawn as `Btn` draws it,
 * until `Btn` takes `wide` (kit request, plan Task 13 report).
 */
const WIDE: CSSProperties = { width: "100%", justifyContent: "center" };
/**
 * Prose in the card wraps without a one-word last line (the detector's
 * `straggler`, found under the enrolment's app hint on 2026-11-25).
 */
const PROSE: CSSProperties = { textWrap: "pretty" };

/**
 * A value the person cannot change, drawn in a field because a password
 * manager reads it there (`autocomplete="username"` on the invitation and the
 * recovery link): on the sunken surface, so it does not look editable. Until
 * the kit styles `.input-field[readonly]` (kit request, plan Task 13 report).
 */
export const READ_ONLY: CSSProperties = { background: "var(--surface-2)" };

/** The tab, named after the screen's headline while the screen is up. */
function TabTitle({ page }: { page: string }) {
  useDocumentTitle(page);
  return null;
}

/**
 * `embedded`: the same frame drawn inside another page -- `/__preview`, where
 * the browser's a11y pass measures it -- as a region named by its headline,
 * not a second `<main>` (which axe refuses), without the full-height ground,
 * and leaving the tab's title to that page.
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
      {embedded ? null : <TabTitle page={title} />}
      <div style={BRAND}>
        <NovieriLockup height={24} />
      </div>
      <div className="panel" style={CARD}>
        <h1 id={headingId} style={HEADLINE}>
          {title}
        </h1>
        {intro ? (
          <p className="note" style={{ ...PROSE, marginTop: 6 }}>
            {intro}
          </p>
        ) : null}
        <div style={{ marginTop: 20, display: "grid", gap: 16 }}>{children}</div>
      </div>
      <div style={FOOT}>
        <div role="group" aria-label={t("common.language.label")} style={{ display: "flex", gap: 8 }}>
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
        <p className="note" style={{ margin: 0 }}>
          {PRODUCT_NAME}
        </p>
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
        <p className="note" style={{ ...PROSE, margin: 0 }}>
          {hint}
        </p>
      ) : null}
      {children}
    </section>
  );
}

/** "or", between two ways through the same step (the password and Microsoft, a passkey and a password). */
export function AuthOr() {
  const t = useT();
  return (
    <p className="note" style={OR}>
      <span aria-hidden="true" style={OR_RULE} />
      <span>{t("auth.common.or")}</span>
      <span aria-hidden="true" style={OR_RULE} />
    </p>
  );
}

/**
 * A step's action across the card (the sign-in pages' "Continue", the
 * passkey, Microsoft): the same `.btn` as `Btn` -- its height, radius,
 * weight, hover, focus ring and disabled state -- stretched to the card.
 */
export function AuthWideBtn({
  label,
  icon,
  variant = "secondary",
  type = "button",
  disabled = false,
  onClick,
}: {
  label: string;
  icon?: string;
  variant?: "primary" | "secondary";
  type?: "button" | "submit";
  disabled?: boolean;
  onClick?: (event: MouseEvent<HTMLButtonElement>) => void;
}) {
  return (
    <button
      type={type}
      className={variant === "primary" ? "btn primary" : "btn"}
      style={WIDE}
      disabled={disabled}
      onClick={onClick}
    >
      {icon ? <Icon name={icon} size={14} /> : null}
      <span>{label}</span>
    </button>
  );
}
