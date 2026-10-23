/**
 * Icon set -- Tabler outline, stroke 1.75, `fill:none`, rendered from the
 * sprite in `./tabler.ts`. No hand-drawn paths: every glyph below is a Tabler
 * id, verbatim.
 *
 * MIT License
 *
 * Copyright (c) 2020-2026 Paweł Kuna
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */

import { TABLER } from "./tabler";

/**
 * The console's own names, kept stable across a Tabler version bump or a
 * swapped glyph, mapped to the Tabler id that currently backs each one. A
 * caller (nav config, a module) never spells out a raw Tabler id -- it asks
 * for `"devices"` or `"warning"`, and this table is the one place that
 * decides which sprite entry answers.
 */
const ALIASES: Record<string, string> = {
  dashboard: "layout-dashboard",
  sparkles: "sparkles",
  devices: "devices",
  health: "heartbeat",
  logs: "logs",
  patch: "shield-check",
  package: "package",
  remote: "user-screen",
  backup: "database",
  image: "photo",
  server: "server-2",
  ticket: "ticket",
  portal: "world",
  bell: "bell",
  mail: "mail",
  clock: "clock",
  book: "book",
  changes: "git-branch",
  bcp: "shield-lock",
  chart: "chart-bar",
  audit: "history",
  license: "license",
  subscription: "credit-card",
  budget: "wallet",
  network: "network",
  jobs: "bolt",
  certificate: "certificate",
  automation: "robot",
  status: "activity",
  settings: "settings",
  search: "search",
  help: "help-circle",
  menu: "menu-2",
  close: "x",
  plus: "plus",
  filter: "filter",
  download: "download",
  upload: "upload",
  play: "player-play",
  pause: "player-pause",
  check: "check",
  warning: "alert-triangle",
  info: "info-circle",
  user: "user",
  users: "users",
  link: "link",
  shield: "shield-check",
  lock: "lock",
  unlock: "lock-open",
  eye: "eye",
  terminal: "terminal-2",
  calendar: "calendar-event",
  globe: "world",
  building: "building",
  database: "database",
  activity: "activity",
  more: "dots",
  chevronRight: "chevron-right",
  chevronLeft: "chevron-left",
  chevronDown: "chevron-down",
};

const warnedOnce = new Set<string>();

export interface IconProps {
  name: string;
  size?: number;
  className?: string;
}

export function Icon({ name, size = 18, className = "" }: IconProps) {
  const tablerId = ALIASES[name] ?? name;
  const markup = TABLER[tablerId];

  if (!markup) {
    if (!warnedOnce.has(name)) {
      warnedOnce.add(name);
      console.warn(`Icon: unknown icon name "${name}"`);
    }
    return null;
  }

  return (
    <svg
      className={["ic", className].filter(Boolean).join(" ")}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      /* Both the alias table and TABLER are static modules -- never a prop,
       * never anything a request or an operator can influence -- so there is
       * no injection surface here. */
      dangerouslySetInnerHTML={{ __html: markup }}
    />
  );
}

/** Every console-facing alias, plus every Tabler id the sprite carries. */
export const ICON_NAMES = [...new Set([...Object.keys(ALIASES), ...Object.keys(TABLER)])];
