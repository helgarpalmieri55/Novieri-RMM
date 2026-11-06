/**
 * `/sign-in/recovery-codes` — the ten recovery codes, shown once (own-auth
 * design §2.2, Q5).
 *
 * They arrive from the step that made them in memory (`./flow`), never in the
 * URL or the router's state, and are dropped when this page goes: a reload
 * shows the "nothing to show" state, which is the truth -- the server keeps
 * only their hashes, so nobody can show them again. New ones are made in
 * Sign-in and security.
 *
 * THE CONSOLE OPENS ONLY AFTER THE PERSON SAYS THEY SAVED THEM. Without the
 * codes, losing a passkey or a phone means another administrator, or the
 * server's recovery command for the only one (§7); the tick costs a second
 * and is the one moment that outcome is still cheap to avoid.
 *
 * Copying does not fail in silence (the enrollment token's rule): a browser
 * that refuses the clipboard says so, and the codes stay on screen to select.
 */
import { useEffect, useState } from "react";

import { useT } from "../../i18n/context";
import { enterConsole } from "../../lib/session";
import { Btn } from "../../shell/components";
import { AuthAlert, AuthCard, AuthDone } from "./AuthCard";
import { forgetCodes, heldCodes, useNext } from "./flow";

async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export function RecoveryCodesPage() {
  const t = useT();
  const next = useNext();
  const [codes] = useState(heldCodes);
  const [saved, setSaved] = useState(false);
  const [copied, setCopied] = useState<"no" | "yes" | "failed">("no");
  // Gone with the page. (Strict mode runs this cleanup once on purpose; the
  // codes are already in this component's state by then.)
  useEffect(() => () => forgetCodes(), []);

  if (codes.length === 0) {
    return (
      <AuthCard title={t("auth.codes.title")}>
        <p className="note" style={{ margin: 0 }}>
          {t("auth.codes.none")}
        </p>
        <div className="form-actions">
          <Btn variant="primary" icon="arrow-right" label={t("auth.codes.continue")} onClick={() => enterConsole(next)} />
        </div>
      </AuthCard>
    );
  }

  return (
    <AuthCard title={t("auth.codes.title")} intro={t("auth.codes.intro")}>
      <ul
        className="code"
        aria-label={t("auth.codes.list")}
        style={{ listStyle: "none", margin: 0, display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 6 }}
      >
        {codes.map((code) => (
          <li key={code}>{code}</li>
        ))}
      </ul>
      <div>
        <Btn
          icon="copy"
          label={t("auth.codes.copy")}
          onClick={() => void copyToClipboard(codes.join("\n")).then((ok) => setCopied(ok ? "yes" : "failed"))}
        />
      </div>
      {copied === "yes" ? <AuthDone>{t("auth.codes.copied")}</AuthDone> : null}
      <AuthAlert text={copied === "failed" ? t("auth.codes.copyFailed") : null} />
      <label className="flex items-center gap-2">
        <input type="checkbox" checked={saved} onChange={(event) => setSaved(event.target.checked)} />
        {t("auth.codes.saved")}
      </label>
      <div className="form-actions">
        <Btn
          variant="primary"
          icon="arrow-right"
          label={t("auth.codes.continue")}
          disabled={!saved}
          onClick={() => {
            forgetCodes();
            enterConsole(next);
          }}
        />
      </div>
    </AuthCard>
  );
}
