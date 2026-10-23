import { createContext, useCallback, useContext, useMemo, useState, useSyncExternalStore, type ReactNode } from "react";
import { getLanguage, setActiveLanguage, subscribe, translate } from "./core";
import type { Language } from "./types";

const STORAGE_KEY = "itms.lang";

interface Ctx { onChange?: (l: Language) => void }
const LanguageContext = createContext<Ctx | null>(null);

/** Re-renders the caller when the language changes, and returns `translate`.
 *  EVERY component that renders translated text calls this, including one
 *  that only calls a helper which uses `translate` internally: the hook is
 *  what subscribes it to the switch. */
export function useT(): typeof translate {
  useSyncExternalStore(subscribe, getLanguage, getLanguage);
  return translate;
}

export function useLanguage(): { language: Language; setLanguage: (l: Language, options?: { persist?: boolean }) => void } {
  const language = useSyncExternalStore(subscribe, getLanguage, getLanguage);
  const ctx = useContext(LanguageContext);
  const setLanguage = useCallback(
    (l: Language, options?: { persist?: boolean }) => {
      try { localStorage.setItem(STORAGE_KEY, l); } catch { /* private mode */ }
      setActiveLanguage(l);
      // `persist: false` is for a caller that is itself saving the account
      // (the profile page's PATCH): it applies the language without asking the
      // provider to save it a second time.
      if (options?.persist !== false) ctx?.onChange?.(l);
    },
    [ctx],
  );
  return { language, setLanguage };
}

export function readStoredLanguage(): string | null {
  try { return localStorage.getItem(STORAGE_KEY); } catch { return null; }
}

export function I18nProvider({ initial, onChange, children }: { initial?: Language; onChange?: (l: Language) => void; children: ReactNode }) {
  // Applied during the first render, not in an effect, so no frame is painted in the wrong language.
  useState(() => { if (initial) setActiveLanguage(initial); });
  const value = useMemo<Ctx>(() => ({ onChange }), [onChange]);
  return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>;
}
