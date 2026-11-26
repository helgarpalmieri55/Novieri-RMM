/**
 * WHAT A PAGE THAT FAILED WHILE DRAWING SHOWS (redesign decision D18). The
 * data router catches a render error at the layout route and draws this in
 * place of the console, instead of React Router's own page ("Unexpected
 * Application Error!", English, inline styles). One sentence that says what
 * failed and that nothing was changed (copy voice), drawn the way `QueryState`
 * draws a failed read, and the way out.
 */
import { Link, useRouteError } from "react-router-dom";

import { useT } from "../i18n/context";
import { Icon } from "./icons";
import { KIND_ICON } from "./viz";

export function RouteError() {
  const t = useT();
  // Read so the router treats the error as handled here. Never shown: a stack
  // is nothing a person can act on, and it can carry a record's data.
  useRouteError();
  return (
    <main className="content route-error" id="main">
      <div role="alert" className="notice red">
        <Icon name={KIND_ICON.crit} size={17} />
        <div>
          <b>{t("shell.routeError.title")}</b>
          <p>{t("shell.routeError.body")}</p>
          <Link className="btn small" to="/dashboard">
            {t("shell.routeError.home")}
          </Link>
        </div>
      </div>
    </main>
  );
}
