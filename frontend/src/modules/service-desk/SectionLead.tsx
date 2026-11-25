import type { ReactNode } from "react";

/**
 * A settings section's sentence and its "New ..." (redesign plan, Task 7,
 * 2.5): a secondary button, because the open dialog's Save is the page's one
 * primary. Shared by the service desk's queues, categories and saved replies
 * and by the notification sections. Module-local until the kit's
 * `FormSection` takes a `description` (the wave's kit request K2), which then
 * replaces it.
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
