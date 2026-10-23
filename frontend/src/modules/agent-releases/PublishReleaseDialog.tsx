import { useRef, useState } from "react";

import { translate } from "../../i18n/core";

import { Button, Dialog, Field, Table, Td, Th, inputClass } from "../../components/ui";
import { useT } from "../../i18n/context";
import { Trans } from "../../i18n/Trans";
import { useApi } from "../../lib/ApiContext";
import { StatusPill } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useInvalidateAgentReleases } from "./api";
import { archLabel, osLabel, platformLabel } from "./platform";
import {
  RELEASE_ARCH,
  RELEASE_OS,
  type PublishItem,
  type PublishOutcome,
  failureText,
  inferPlatform,
  parseSignatures,
  planProblems,
  runPublish,
} from "./publish";

/**
 * "Publish a release": the console replacement for the two `curl` calls and
 * the `PUT release-signature` that `deploy/README.md` used to be the only way
 * to make.
 *
 * WHO SEES IT. The page renders the button only for a holder of
 * `agent.release.manage`, as a convenience; every call below is refused
 * server-side for anyone else (invariant 6).
 *
 * WHAT IT DOES NOT DO. It builds nothing and signs nothing. The binaries and
 * the SIGNATURES file come from the release workflow's draft GitHub release;
 * the release key never leaves that workflow, and this dialog only carries its
 * output to the catalogue -- refusing to attach a signature to bytes it does
 * not cover (see `publish.ts`).
 */

/** The release workflow's side files: downloaded with the binaries, not binaries. */
const SIDE_FILES = new Set(["SHA256SUMS", "release-signing-key.pub"]);
const SIGNATURES_NAME = "SIGNATURES";

function formatSize(bytes: number): string {
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

/**
 * The signatures file as it was read: its name and its TEXT. The usable lines
 * and the sentences about the unusable ones are worked out at render
 * (`parseSignatures`), so a language switch re-words the sentences.
 */
interface SignaturesState {
  name: string;
  text: string;
}

function StepPill({ outcome }: { outcome: PublishOutcome }) {
  const t = useT();
  switch (outcome.step) {
    case "waiting":
      return <StatusPill value="waiting" kind="mut" label={t("agentReleases.publish.stepWaiting")} />;
    case "creating":
      return <StatusPill value="creating" kind="run" label={t("agentReleases.publish.stepCreating")} />;
    case "uploading":
      return <StatusPill value="uploading" kind="run" label={t("agentReleases.publish.stepUploading")} />;
    case "signing":
      return <StatusPill value="signing" kind="run" label={t("agentReleases.publish.stepSigning")} />;
    case "failed":
      return <StatusPill value="failed" kind="crit" label={t("agentReleases.publish.stepFailed")} />;
    case "done":
      return outcome.signed ? (
        <StatusPill value="signed" kind="ok" label={t("agentReleases.publish.stepSigned")} />
      ) : (
        <StatusPill value="unsigned" kind="warn" label={t("agentReleases.publish.stepUnsigned")} />
      );
  }
}

/** The failure is worded here, from its data, so it follows the language. */
function outcomeDetail(outcome: PublishOutcome): string | null {
  if (outcome.failure) return failureText(outcome.failure);
  if (outcome.error) return outcome.error;
  if (outcome.step !== "done" || outcome.signed) return null;
  if (outcome.signature === "no-line") return translate("agentReleases.publish.detailNoLine");
  if (outcome.signature === "no-file") return translate("agentReleases.publish.detailNoFile");
  return null;
}

export function PublishReleaseDialog({ onClose }: { onClose: () => void }) {
  const t = useT();
  const api = useApi();
  const invalidate = useInvalidateAgentReleases();
  const binariesInput = useRef<HTMLInputElement>(null);
  const signaturesInput = useRef<HTMLInputElement>(null);

  const [version, setVersion] = useState("");
  const [items, setItems] = useState<PublishItem[]>([]);
  const [skipped, setSkipped] = useState<string[]>([]);
  const [signatures, setSignatures] = useState<SignaturesState | null>(null);
  const [tried, setTried] = useState(false);
  const [outcomes, setOutcomes] = useState<PublishOutcome[] | null>(null);
  const [running, setRunning] = useState(false);

  // Worked out from the file's text at render, never held (see SignaturesState).
  const parsed = signatures ? parseSignatures(signatures.text) : null;
  const problems = planProblems(version.trim(), items);
  const finished = outcomes != null && !running;
  const locked = running || finished;

  async function readSignatures(file: File) {
    const text = await file.text();
    setSignatures({ name: file.name, text });
  }

  function chooseBinaries(files: File[]) {
    const binaries: PublishItem[] = [];
    const left: string[] = [];
    for (const file of files) {
      // Selecting every downloaded asset at once is the natural thing to do,
      // so the side files are recognised rather than offered as binaries to
      // be given a platform.
      if (file.name === SIGNATURES_NAME) {
        void readSignatures(file);
      } else if (SIDE_FILES.has(file.name) || file.name.endsWith(".sig")) {
        left.push(file.name);
      } else {
        binaries.push({ file, ...(inferPlatform(file.name) ?? { os: "", arch: "" }) });
      }
    }
    setItems(binaries);
    setSkipped(left);
  }

  function setPlatform(index: number, patch: Partial<PublishItem>) {
    setItems((current) => current.map((item, i) => (i === index ? { ...item, ...patch } : item)));
  }

  async function publish() {
    setTried(true);
    if (problems.length > 0) return;
    setRunning(true);
    try {
      await runPublish(api, version.trim(), items, parsed?.entries ?? null, setOutcomes);
    } finally {
      setRunning(false);
      invalidate();
    }
  }

  const published = (outcomes ?? []).filter((o) => o.uploaded).length;

  return (
    // NOT CLOSABLE WHILE RUNNING. The run carries on without the dialog, and
    // an operator who closed it would not see which files failed.
    <Dialog open title={t("agentReleases.publish.title")} onClose={running ? () => {} : onClose} dismissible={!running}>
      <p>
        <Trans k="agentReleases.publish.intro" components={{ mono: <span className="mono" /> }} />
      </p>

      <div className="form-grid mt-3">
        <Field
          label={t("agentReleases.publish.version")}
          full
          help={t("agentReleases.publish.versionHelp")}
        >
          <input
            className={inputClass}
            value={version}
            disabled={locked}
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => setVersion(event.target.value)}
          />
        </Field>
      </div>

      <input
        ref={binariesInput}
        type="file"
        multiple
        className="hidden"
        aria-label={t("agentReleases.publish.binariesLabel")}
        onChange={(event) => chooseBinaries([...(event.target.files ?? [])])}
      />
      <input
        ref={signaturesInput}
        type="file"
        className="hidden"
        aria-label={t("agentReleases.publish.signaturesLabel")}
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void readSignatures(file);
        }}
      />
      <div className="mt-3 flex flex-wrap gap-2">
        <Button variant="outline" disabled={locked} onClick={() => binariesInput.current?.click()}>
          <Icon name="upload" size={14} />
          <span>{items.length > 0 ? t("agentReleases.publish.chooseOtherBinaries") : t("agentReleases.publish.chooseBinaries")}</span>
        </Button>
        <Button variant="outline" disabled={locked} onClick={() => signaturesInput.current?.click()}>
          <Icon name="upload" size={14} />
          <span>{signatures ? t("agentReleases.publish.chooseOtherSignatures") : t("agentReleases.publish.chooseSignatures")}</span>
        </Button>
      </div>

      {skipped.length > 0 && (
        <p className="note mt-2">
          <Trans
            k="agentReleases.publish.skipped"
            vars={{ files: skipped.join(", ") }}
            components={{ mono: <span className="mono" /> }}
          />
        </p>
      )}

      {signatures && parsed ? (
        <p className="note mt-2">
          <Trans
            k="agentReleases.publish.signaturesRead"
            vars={{ name: signatures.name, count: parsed.entries.length }}
            components={{ mono: <span className="mono" /> }}
          />
        </p>
      ) : (
        <p className="note mt-2">{t("agentReleases.publish.noSignatures")}</p>
      )}
      {parsed && parsed.problems.length > 0 && (
        <ul role="alert" className="form-error mt-1">
          {parsed.problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      )}

      {items.length > 0 && (
        <Table label={t("agentReleases.publish.filesTable")}>
          <thead>
            <tr>
              <Th>{t("agentReleases.publish.file")}</Th>
              <Th>{t("agentReleases.publish.platform")}</Th>
              <Th>{t("agentReleases.publish.size")}</Th>
              <Th>{outcomes ? t("agentReleases.publish.result") : t("agentReleases.publish.releaseSignature")}</Th>
            </tr>
          </thead>
          <tbody>
            {items.map((item, index) => {
              const known = inferPlatform(item.file.name) != null;
              const outcome = outcomes?.[index];
              const detail = outcome ? outcomeDetail(outcome) : null;
              const line = parsed?.entries.find((e) => e.os === item.os && e.arch === item.arch);
              return (
                <tr key={`${item.file.name}-${index}`}>
                  <Td className="mono">{item.file.name}</Td>
                  <Td>
                    {known ? (
                      platformLabel(item.os, item.arch)
                    ) : (
                      <div className="flex gap-2">
                        <select
                          className={inputClass}
                          aria-label={t("agentReleases.publish.osFor", { name: item.file.name })}
                          value={item.os}
                          disabled={locked}
                          onChange={(event) => setPlatform(index, { os: event.target.value })}
                        >
                          <option value="">{t("agentReleases.publish.osOption")}</option>
                          {RELEASE_OS.map((os) => (
                            <option key={os} value={os}>
                              {osLabel(os)}
                            </option>
                          ))}
                        </select>
                        <select
                          className={inputClass}
                          aria-label={t("agentReleases.publish.processorFor", { name: item.file.name })}
                          value={item.arch}
                          disabled={locked}
                          onChange={(event) => setPlatform(index, { arch: event.target.value })}
                        >
                          <option value="">{t("agentReleases.publish.processorOption")}</option>
                          {RELEASE_ARCH.map((arch) => (
                            <option key={arch} value={arch}>
                              {archLabel(item.os, arch)}
                            </option>
                          ))}
                        </select>
                      </div>
                    )}
                  </Td>
                  <Td className="mono whitespace-nowrap">{formatSize(item.file.size)}</Td>
                  <Td>
                    {outcome ? (
                      <>
                        <StepPill outcome={outcome} />
                        {detail && (
                          <div className={outcome.step === "failed" ? "form-error" : "cell-sub"}>
                            {detail}
                          </div>
                        )}
                      </>
                    ) : !signatures ? (
                      <span className="cell-sub">{t("agentReleases.publish.noSignaturesFile")}</span>
                    ) : line ? (
                      <span className="cell-sub">{t("agentReleases.publish.foundChecked")}</span>
                    ) : (
                      <span className="cell-sub">{t("agentReleases.publish.noLine")}</span>
                    )}
                  </Td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      )}

      <p className="note mt-3">
        <Trans k="agentReleases.publish.notCodeSigned" components={{ strong: <strong /> }} />
      </p>
      <p className="note mt-2">
        <Trans k="agentReleases.publish.startsPilot" components={{ strong: <strong /> }} />
      </p>

      {tried && problems.length > 0 && (
        <ul role="alert" className="form-error mt-3">
          {problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      )}

      {finished && (
        <p role="status" className="mt-3">
          {t("agentReleases.publish.published", { published, count: items.length })}
        </p>
      )}

      <div className="mt-3 flex justify-end gap-2">
        {finished ? (
          <Button onClick={onClose}>{t("agentReleases.publish.close")}</Button>
        ) : (
          <>
            <Button variant="outline" disabled={running} onClick={onClose}>
              {t("agentReleases.publish.cancel")}
            </Button>
            <Button disabled={running} onClick={() => void publish()}>
              {running ? t("agentReleases.publish.publishing") : t("agentReleases.publish.publish")}
            </Button>
          </>
        )}
      </div>
    </Dialog>
  );
}
