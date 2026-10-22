import { cloneElement, Fragment, type ReactElement } from "react";
import type { MessageKey } from "./core";
import { useT } from "./context";

/** `<link>text</link>` markers in the catalogue string become the matching
 *  element from `components`, with the inner text as its child. No HTML is
 *  parsed; an unknown marker renders as plain text. */
export function Trans({ k, vars, components }: { k: MessageKey; vars?: Record<string, string | number>; components: Record<string, ReactElement> }) {
  const t = useT();
  // Markers are found in the CATALOGUE's text, before any value is filled in:
  // a value (a hostname an agent reported, a name a person typed) that happens
  // to contain `<link>…</link>` stays text and never becomes an element. Each
  // value is held as a token through the split and put back afterwards;
  // `count` stays a number, because it chooses the plural.
  const held: string[] = [];
  const tokens: Record<string, string | number> = {};
  for (const [name, value] of Object.entries(vars ?? {})) {
    if (name === "count") {
      tokens[name] = value;
    } else {
      tokens[name] = `\u0000${held.length}\u0000`;
      held.push(String(value));
    }
  }
  const fill = (text: string) => text.replace(/\u0000(\d+)\u0000/g, (_whole, index: string) => held[Number(index)] ?? "");
  const parts = t(k, tokens).split(/(<(\w+)>.*?<\/\2>)/g);
  const out: ReactElement[] = [];
  for (let i = 0; i < parts.length; i += 1) {
    const part = parts[i] ?? "";
    const match = /^<(\w+)>(.*)<\/\1>$/.exec(part);
    if (match) {
      const element = components[match[1]!];
      out.push(element ? cloneElement(element, { key: i }, fill(match[2]!)) : <Fragment key={i}>{fill(part)}</Fragment>);
      i += 1; // skip the captured tag name
    } else if (part) {
      out.push(<Fragment key={i}>{fill(part)}</Fragment>);
    }
  }
  return <>{out}</>;
}
