import type { ReactNode } from "react";

/**
 * A settings section's sentence and its "New ..." (redesign plan, Task 7,
 * 2.5): a secondary button, because the open dialog's Save is the page's one
 * primary. Shared by the service desk's queues, categories and saved replies
 * and by the notification rules, channels and allowlist.
 *
 * STILL LOCAL, ON PURPOSE (adoption pass, 2026-11-25). The kit's
 * `FormSection description` draws the sentence alone; every section here
 * also carries its "New ..." beside it, which the kit does not cover. A lead
 * with no action is `FormSection description` (Queue members).
 */
export function SectionLead({ text, action }: { text: string; action?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2" style={{ marginBottom: 12 }}>
      <p className="note" style={{ margin: 0 }}>
        {text}
      </p>
      {action ?? null}
    </div>
  );
}
