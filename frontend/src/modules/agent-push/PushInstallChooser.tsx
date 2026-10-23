// "Push install the agent…": ticked machines, or a subnet's list (design
// 2026-09-30 §1.1). Two ways to NAME machines, and each ends in a person
// confirming named machines. The first keeps the batch ceiling of 25; the
// second is a run, at most 1,024 confirmed machines, 50 at a time
// (CLAUDE.md 2a(f) as amended 2026-09-30).

import { useState } from "react";

import { useT } from "../../i18n/context";
import { Button } from "../../components/ui";
import type { Device } from "../../lib/api";
import { Panel } from "../../shell/components";
import { PushBySubnetFlow } from "./PushBySubnetFlow";
import { PushInstallFlow } from "./PushInstallFlow";
import { PUSH_BATCH_MAX, PUSH_RUN_CONCURRENCY } from "./types";

export function PushInstallChooser({
  machines,
  onRemove,
  onClose,
}: {
  machines: readonly Device[];
  onRemove: (id: string) => void;
  onClose: () => void;
}) {
  const t = useT();
  const [mode, setMode] = useState<"ticked" | "subnet">(machines.length > 0 ? "ticked" : "subnet");
  return (
    <div className="space-y-4">
      <Panel
        title={t("agentPush.chooser.title")}
        actions={
          <Button variant="ghost" onClick={onClose}>
            {t("agentPush.shared.close")}
          </Button>
        }
      >
        <fieldset className="field full">
          <legend className="note" style={{ marginBottom: 6, fontWeight: 500 }}>
            {t("agentPush.chooser.which")}
          </legend>
          <div className="flex flex-col gap-1.5">
            <label className="flex items-center gap-2">
              <input
                type="radio"
                name="push-choose-machines"
                value="ticked"
                checked={mode === "ticked"}
                onChange={() => setMode("ticked")}
              />
              <span>
                {t("agentPush.chooser.ticked")}{" "}
                <span className="note">{t("agentPush.chooser.tickedNote", { max: PUSH_BATCH_MAX })}</span>
              </span>
            </label>
            <label className="flex items-center gap-2">
              <input
                type="radio"
                name="push-choose-machines"
                value="subnet"
                checked={mode === "subnet"}
                onChange={() => setMode("subnet")}
              />
              <span>
                {t("agentPush.chooser.bySubnet")}{" "}
                <span className="note">{t("agentPush.chooser.bySubnetNote", { count: PUSH_RUN_CONCURRENCY })}</span>
              </span>
            </label>
          </div>
        </fieldset>
      </Panel>
      {mode === "subnet" ? (
        <PushBySubnetFlow onClose={onClose} />
      ) : machines.length > 0 ? (
        <PushInstallFlow machines={machines} onRemove={onRemove} onClose={onClose} />
      ) : (
        <p className="note">{t("agentPush.chooser.tickPrompt")}</p>
      )}
    </div>
  );
}
