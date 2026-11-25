/**
 * THE PAGE'S SETTINGS: WHETHER EACH RULE ROUTES AND EACH CHANNEL SENDS
 * (redesign plan, Task 7, 2.8; Receta FO). Both on/off switches used to write
 * the moment they were thrown, one row at a time. Now every switch on the
 * page is one draft, saved by the page's one save bar and discarded by it,
 * and leaving with a switch thrown asks first. The save sends, per switch
 * that changed, the same PATCH the switch used to send at once: the rule's
 * `{ active }` and the channel's `{ active }` (no secret: a channel's
 * credential is written in its dialog and never read back, invariant 3).
 *
 * A refusal keeps the sentence of today in the panel the switch is in ("The
 * rule was not changed. ...", "The channel was not changed. ..."), and the
 * change stays unsaved (`markSent(null)`).
 */
import { createContext, useContext, useMemo, useState } from "react";

import { useDraft } from "../../shell/components";
import { useUpdateNotificationChannelMutation, useUpdateNotificationRuleMutation } from "./api";
import type { ChannelOut, RuleOut } from "./types";

type Kind = "rule" | "channel";

interface SwitchState {
  readonly rule: Readonly<Record<string, boolean>>;
  readonly channel: Readonly<Record<string, boolean>>;
}

export interface Switches {
  /** The switch as the draft holds it (`saved`: the row's own state, before the draft knows it). */
  active(kind: Kind, id: string, saved: boolean): boolean;
  toggle(kind: Kind, id: string): void;
  readonly saving: boolean;
  /** The refused save of a rule switch, or of a channel switch, for its panel's own sentence. */
  readonly ruleError: unknown;
  readonly channelError: unknown;
}

const SwitchesContext = createContext<Switches | null>(null);
export const SwitchesProvider = SwitchesContext.Provider;

/** The page's switches, for a panel inside `SwitchesProvider`. */
export function useSwitches(): Switches {
  const switches = useContext(SwitchesContext);
  if (!switches) throw new Error("useSwitches outside NotificationAdminPage's SwitchesProvider");
  return switches;
}

function byId(rows: readonly { id: string; active: boolean }[] | undefined): Record<string, boolean> {
  return Object.fromEntries((rows ?? []).map((row) => [row.id, row.active]));
}

export function useSwitchDraft(rules: readonly RuleOut[] | undefined, channels: readonly ChannelOut[] | undefined) {
  const saved = useMemo<SwitchState>(() => ({ rule: byId(rules), channel: byId(channels) }), [rules, channels]);
  const { draft, setDraft, dirty, discard, markSent } = useDraft<SwitchState>(saved);
  const updateRule = useUpdateNotificationRuleMutation();
  const updateChannel = useUpdateNotificationChannelMutation();
  const [saving, setSaving] = useState(false);

  async function save() {
    if (!draft) return;
    setSaving(true);
    updateRule.reset();
    updateChannel.reset();
    markSent(draft);
    try {
      for (const [id, active] of Object.entries(draft.rule)) {
        if (saved.rule[id] !== undefined && saved.rule[id] !== active) await updateRule.mutateAsync({ id, body: { active } });
      }
      for (const [id, active] of Object.entries(draft.channel)) {
        if (saved.channel[id] !== undefined && saved.channel[id] !== active) {
          await updateChannel.mutateAsync({ id, body: { active } });
        }
      }
    } catch {
      // Announced in the switch's own panel; the change stays unsaved.
      markSent(null);
    } finally {
      setSaving(false);
    }
  }

  const switches: Switches = {
    active: (kind, id, fallback) => draft?.[kind][id] ?? fallback,
    toggle: (kind, id) => {
      if (!draft) return;
      const current = draft[kind][id];
      if (current === undefined) return;
      setDraft({ ...draft, [kind]: { ...draft[kind], [id]: !current } });
    },
    saving,
    ruleError: updateRule.error,
    channelError: updateChannel.error,
  };
  return {
    switches,
    dirty,
    saving,
    save: () => void save(),
    discard: () => {
      updateRule.reset();
      updateChannel.reset();
      discard();
    },
  };
}
