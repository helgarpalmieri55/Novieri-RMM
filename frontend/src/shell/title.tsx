/**
 * THE BROWSER TAB SAYS WHERE YOU ARE (design §5; WCAG 2.4.2): "<Page> ·
 * Novieri RMM", and on a record "<Record> · <Page> · Novieri RMM". Atera's own
 * walkthrough files "the same title on every page" as a defect, and a person
 * with a dozen consoles open reads the tab before anything else.
 *
 * The shell owns the page half (its title is the rail entry's own label, so
 * it cannot drift from the rail and the crumb); a record page adds its name
 * through `useRecordTitle`. A frame with no shell -- sign-in, the portal, the
 * consent page -- names itself with `useDocumentTitle`.
 */
import { createContext, useContext, useEffect } from "react";

import { PRODUCT_NAME } from "./brand";

export function documentTitle(page?: string, record?: string): string {
  const parts = [record, page]
    .map((part) => part?.trim() ?? "")
    .filter((part) => part !== "" && part !== PRODUCT_NAME);
  return [...parts, PRODUCT_NAME].join(" · ");
}

const RecordTitle = createContext<((name: string | undefined) => void) | null>(null);

/** Given by `AppShell`, which holds the record's name beside the page's title. */
export const RecordTitleProvider = RecordTitle.Provider;

/** A record page names itself in the tab while it is open. Outside the shell, nothing. */
export function useRecordTitle(name: string | undefined): void {
  const set = useContext(RecordTitle);
  useEffect(() => {
    if (!set) return undefined;
    set(name);
    return () => set(undefined);
  }, [set, name]);
}

/** A frame with no shell names its page, and gives the previous title back when it goes. */
export function useDocumentTitle(page: string | undefined): void {
  useEffect(() => {
    const previous = document.title;
    document.title = documentTitle(page);
    return () => {
      document.title = previous;
    };
  }, [page]);
}
