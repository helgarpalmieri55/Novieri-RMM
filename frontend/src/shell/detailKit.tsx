/**
 * THE RECORD PATTERN (design §2, "Ficha"; Atera's device page, capture "atera
 * 8"): a head with the record's status, context and actions; summary tiles;
 * one row of tabs that change the URL, so a tab reloads and links -- which
 * Atera's do not; and, where a record has both, information on the left and
 * metrics and activity on the right.
 */
import { useCallback, useId, type ReactNode } from "react";
import { Link, NavLink, useSearchParams } from "react-router-dom";

import { useClientScope } from "../lib/clientScope";
import { Tabs } from "./components";
import { Icon } from "./icons";
import { Pill, type Kind } from "./viz";
import "./kit.css";

export function DetailHead({
  back,
  title,
  status,
  context,
  actions,
}: {
  back: { to: string; label: string };
  title: string;
  status?: { kind: Kind; label: string };
  context?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <section className="ph detail-head">
      <Link className="back-link" to={back.to}>
        <Icon name="chevron-left" size={14} />
        {back.label}
      </Link>
      <div className="ph-row">
        <div>
          <h1>{title}</h1>
          {status || context ? (
            <div className="detail-meta">
              {status ? <Pill kind={status.kind}>{status.label}</Pill> : null}
              {context ? <span className="detail-context">{context}</span> : null}
            </div>
          ) : null}
        </div>
        {actions ? <div className="ph-act">{actions}</div> : null}
      </div>
    </section>
  );
}

/**
 * The tab a record shows, from `?tab=` (or `param`). The default tab writes
 * nothing; an unknown one falls back to it. A change is a NEW history entry,
 * so Back undoes a tab change instead of leaving the record.
 */
export function useUrlTab<K extends string>(keys: readonly K[], fallback: K, param = "tab"): [K, (key: K) => void] {
  const [params, setParams] = useSearchParams();
  const requested = params.get(param) ?? "";
  const active = (keys as readonly string[]).includes(requested) ? (requested as K) : fallback;
  const select = useCallback(
    (key: K) =>
      setParams((current) => {
        const next = new URLSearchParams(current);
        if (key === fallback) next.delete(param);
        else next.set(param, key);
        return next;
      }),
    [setParams, fallback, param],
  );
  return [active, select];
}

/** One row of tabs (it scrolls; it never wraps into two, Atera's flaw) and the panel they switch. */
export function UrlTabs<K extends string>({
  items,
  active,
  onSelect,
  label,
  children,
}: {
  items: readonly { key: K; label: string; count?: number }[];
  active: K;
  onSelect: (key: K) => void;
  label: string;
  children: ReactNode;
}) {
  const panelId = useId();
  const current = items.find((item) => item.key === active);
  return (
    <>
      <Tabs items={items} active={active} onSelect={(key) => onSelect(key as K)} panelId={panelId} label={label} scroll />
      <div id={panelId} role="tabpanel" aria-label={current?.label} tabIndex={-1} className="tab-panel">
        {children}
      </div>
    </>
  );
}

/** Information on the left, metrics and activity on the right; one column below 1024px. */
export function DetailGrid({ main, aside }: { main: ReactNode; aside: ReactNode }) {
  return (
    <div className="detail-grid">
      <div className="detail-main">{main}</div>
      <div className="detail-aside">{aside}</div>
    </div>
  );
}

/**
 * A MODULE'S OWN PAGES AS ONE ROW OF LINKS (deploy, backup, IPAM, licenses,
 * provisioning, push install, the service desk, the portal). Eight modules
 * drew their own copy of this strip, each a `role="tablist"` of links -- a
 * tablist promises arrow keys and panels that links do not have. This is a
 * plain navigation: `NavLink` marks the current page with `aria-current`.
 */
export function SectionNav({
  label,
  items,
}: {
  label: string;
  items: readonly { to: string; label: string; end?: boolean }[];
}) {
  return (
    <nav aria-label={label} className="tabs scroll section-nav">
      {items.map((item) => (
        <NavLink key={item.to} to={item.to} end={item.end} className={({ isActive }) => (isActive ? "tab active" : "tab")}>
          {item.label}
        </NavLink>
      ))}
    </nav>
  );
}

/** A record's client (linked to its 360 page) and site, for `DetailHead`'s context. */
export function ClientContext({ clientId, siteName }: { clientId: string | null; siteName?: string | null }) {
  const { clients } = useClientScope();
  const client = clientId === null ? undefined : clients.find((item) => item.id === clientId);
  if (!client && !siteName) return null;
  return (
    <>
      {client ? <Link to={`/clients/${client.id}`}>{client.name}</Link> : null}
      {client && siteName ? " · " : null}
      {siteName ?? null}
    </>
  );
}
