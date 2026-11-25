/**
 * A PORTAL PAGE NAMES THE BROWSER TAB, BUT ONLY IN THE CONTACT'S OWN FRAME
 * (redesign plan, Task 7, "El marco del portal"; design §5). The portal has no
 * `AppShell`, so each of its pages names itself with `useDocumentTitle`:
 * "My tickets · Novieri RMM", "INC-4127 · Novieri RMM" (a ticket by its number,
 * which carries its own prefix).
 *
 * NOT ANYWHERE ELSE. A technician who holds a portal code opens the same pages
 * inside the console, where `AppShell` already names the tab from its rail;
 * a second writer there would leave the tab on the page's name after Back.
 * And `/__preview` draws the frame `embedded`, a picture of it that, like the
 * embedded `AuthCard`, leaves the developer's tab alone. So `PortalShell`
 * says whether its pages name the tab, and only it does.
 */
import { createContext, useContext } from "react";

import { useDocumentTitle } from "../../shell/title";

/** True inside the contact's own `PortalShell`; false embedded, under the console's shell, and bare. */
export const PortalNamesTab = createContext(false);

function TabTitle({ page }: { page: string }) {
  useDocumentTitle(page);
  return null;
}

/** The page's name in the tab, while it is drawn in the contact's frame. `undefined`: not known yet. */
export function PortalTitle({ page }: { page: string | undefined }) {
  const names = useContext(PortalNamesTab);
  return names && page ? <TabTitle page={page} /> : null;
}
