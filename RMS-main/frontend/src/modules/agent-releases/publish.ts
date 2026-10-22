/**
 * Publishing an agent release from the console: the logic, kept free of React
 * so every rule below is tested without a dialog in the way.
 *
 * WHAT THE INPUT IS. `.github/workflows/release-agent.yml` drafts a GitHub
 * release carrying six binaries named `its-agent-<os>-<arch>[.exe]` and a
 * `SIGNATURES` file, one line per binary, written by
 * `agent/cmd/release-sign/main.go` as
 *
 *     <sha256>  <filename>  <os>/<arch>  <base64 Ed25519 signature>
 *
 * WHY THE DIGEST COMPARISON IS HERE AND NOT ON THE SERVER. The signature is
 * over (version, os, arch, sha256). The control plane never holds the release
 * key and does not verify it (`agent_update.service.set_release_signature`
 * stores what it is given); the agent verifies it, at self-update time, on the
 * endpoint. A signature recorded against the wrong bytes therefore looks fine
 * in the catalogue and fails in the field. The sha256 in the signatures line is
 * what the key signed, and the sha256 the upload returns is what the SERVER
 * computed over the bytes it stored -- if they differ, the signature cannot
 * verify, and sending it would only record a claim that is false.
 */

import { describeSubmitError } from "../../components/form";
import { translate } from "../../i18n/core";
import type { AgentRelease, AgentReleaseCreate } from "../../lib/api";
import { platformLabel } from "./platform";

/** The largest artefact the server accepts (`agent_distribution/api.py`). */
export const MAX_ARTIFACT_BYTES = 128 * 1024 * 1024;

/** The sets `app/core/storage.py` accepts and `build.sh` produces. */
export const RELEASE_OS = ["linux", "windows", "darwin"] as const;
export const RELEASE_ARCH = ["amd64", "arm64"] as const;

/**
 * The release workflow's version check, verbatim. Agents self-update only
 * between versions they can order, so a git SHA or a `v` prefix is refused
 * there -- and refused here, before a row is created that can never be
 * rolled out.
 */
const STRICT_SEMVER =
  /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$/;

export function isStrictSemver(version: string): boolean {
  return STRICT_SEMVER.test(version);
}

export interface Platform {
  os: string;
  arch: string;
}

/**
 * The platform a binary's name says it is for, or null.
 *
 * The same rule as `release-sign`'s `platformOf`: strip `.exe`, then exactly
 * `its-agent-<os>-<arch>` with both halves from the known sets. Anything else
 * -- a browser's "(1)" suffix, an `x86_64` alias -- is left for the operator
 * to name rather than guessed at.
 */
export function inferPlatform(filename: string): Platform | null {
  const parts = filename.replace(/\.exe$/, "").split("-");
  if (parts.length !== 4 || parts[0] !== "its" || parts[1] !== "agent") return null;
  const [, , os, arch] = parts;
  if (!(RELEASE_OS as readonly string[]).includes(os!)) return null;
  if (!(RELEASE_ARCH as readonly string[]).includes(arch!)) return null;
  return { os: os!, arch: arch! };
}

export interface SignatureLine extends Platform {
  sha256: string;
  filename: string;
  signature: string;
}

/** `ReleaseSignatureIn`'s pattern: 64 bytes of Ed25519, base64. */
const SIGNATURE = /^[A-Za-z0-9+/]{86}==$/;
const SHA256 = /^[0-9a-fA-F]{64}$/;

/**
 * Every usable line of a SIGNATURES file, and a sentence for each that is not.
 *
 * A line that cannot be read is REPORTED, not skipped: skipping it would
 * publish that platform with no release signature and no word as to why. Two
 * lines for one platform are both dropped, because choosing one would be the
 * console deciding which signature is real.
 */
export function parseSignatures(text: string): { entries: SignatureLine[]; problems: string[] } {
  const parsed: SignatureLine[] = [];
  const problems: string[] = [];
  text.split(/\r?\n/).forEach((raw, index) => {
    const line = raw.trim();
    if (!line) return;
    const fields = line.split(/\s+/);
    const [sha256, filename, platform, signature] = fields;
    const [os, arch, ...extra] = (platform ?? "").split("/");
    if (
      fields.length !== 4 ||
      !SHA256.test(sha256!) ||
      !SIGNATURE.test(signature!) ||
      extra.length > 0 ||
      !(RELEASE_OS as readonly string[]).includes(os!) ||
      !(RELEASE_ARCH as readonly string[]).includes(arch ?? "")
    ) {
      problems.push(translate("agentReleases.publishRules.badLine", { line: index + 1 }));
      return;
    }
    parsed.push({ sha256: sha256!.toLowerCase(), filename: filename!, os: os!, arch: arch!, signature: signature! });
  });

  const key = (p: Platform) => `${p.os}/${p.arch}`;
  const counts = new Map<string, number>();
  for (const entry of parsed) counts.set(key(entry), (counts.get(key(entry)) ?? 0) + 1);
  const entries = parsed.filter((entry) => counts.get(key(entry)) === 1);
  const reported = new Set<string>();
  for (const entry of parsed) {
    if (counts.get(key(entry))! > 1 && !reported.has(key(entry))) {
      reported.add(key(entry));
      problems.push(
        translate("agentReleases.publishRules.duplicate", { platform: platformLabel(entry.os, entry.arch) }),
      );
    }
  }
  return { entries, problems };
}

export type SignDecision =
  | { kind: "sign"; signature: string }
  | { kind: "mismatch"; expected: string; actual: string }
  | { kind: "no-line" }
  | { kind: "no-file" };

/**
 * Whether to send a signature for one uploaded artefact.
 *
 * `uploadedSha` MUST be the digest the server returned for the upload, never
 * one computed in this browser: the point is to compare what was signed with
 * what the platform will hand out.
 */
export function decideSignature(
  entries: SignatureLine[] | null,
  os: string,
  arch: string,
  uploadedSha: string,
): SignDecision {
  if (entries == null) return { kind: "no-file" };
  const line = entries.find((entry) => entry.os === os && entry.arch === arch);
  if (!line) return { kind: "no-line" };
  const actual = uploadedSha.toLowerCase();
  if (line.sha256 !== actual) return { kind: "mismatch", expected: line.sha256, actual };
  return { kind: "sign", signature: line.signature };
}

/** One binary the operator chose, and the platform it will be published as. */
export interface PublishItem {
  file: File;
  /** Empty until the operator picks one for a name that was not recognised. */
  os: string;
  arch: string;
}

/** Why the form cannot be submitted yet, as sentences. Empty means it can. */
export function planProblems(version: string, items: PublishItem[]): string[] {
  const problems: string[] = [];
  if (version.trim() === "") {
    problems.push(translate("agentReleases.publishRules.enterVersion"));
  } else if (!isStrictSemver(version)) {
    problems.push(translate("agentReleases.publishRules.badVersion"));
  }
  if (items.length === 0) problems.push(translate("agentReleases.publishRules.chooseBinary"));
  const seen = new Map<string, number>();
  for (const item of items) {
    if (!item.os || !item.arch) {
      problems.push(translate("agentReleases.publishRules.choosePlatform", { name: item.file.name }));
      continue;
    }
    const key = `${item.os}/${item.arch}`;
    seen.set(key, (seen.get(key) ?? 0) + 1);
    if (item.file.size > MAX_ARTIFACT_BYTES) {
      problems.push(translate("agentReleases.publishRules.tooLarge", { name: item.file.name }));
    }
  }
  for (const [key, count] of seen) {
    if (count > 1) {
      const [os, arch] = key.split("/");
      problems.push(translate("agentReleases.publishRules.twoFiles", { platform: platformLabel(os!, arch!) }));
    }
  }
  return problems;
}

/** Where one file has got to. `done` and `failed` are final. */
export interface PublishOutcome {
  step: "waiting" | "creating" | "uploading" | "signing" | "done" | "failed";
  /** A catalogue entry exists for this file (and was not cleaned up). */
  created: boolean;
  uploaded: boolean;
  /** A release signature was recorded for it. */
  signed: boolean;
  /** Why no signature was sent, when none was. */
  signature?: SignDecision["kind"];
  /** The server's digest of what it stored. */
  sha256?: string;
  /**
   * Why it failed, in the language active when it failed. Kept for callers that
   * read a sentence; the dialog does NOT hold or show this one, it words
   * `failure` at render so a language switch re-words it.
   */
  error?: string;
  /** The same failure as data: the cause and what became of the entry. */
  failure?: PublishFailure;
}

/** What went wrong with one file, as data (never as a held sentence). */
export type PublishFailure =
  | { kind: "create"; cause: unknown }
  | { kind: "upload"; cause: unknown; removed: boolean }
  | { kind: "mismatch"; removed: boolean }
  | { kind: "signature"; cause: unknown; removed: boolean };

/** The sentence for a failure, in the language active now. */
export function failureText(failure: PublishFailure): string {
  switch (failure.kind) {
    case "create":
      return describeSubmitError(failure.cause);
    case "upload":
      return translate(
        failure.removed ? "agentReleases.publishRules.emptyRemoved" : "agentReleases.publishRules.emptyNotRemoved",
        { reason: describeSubmitError(failure.cause) },
      );
    case "mismatch":
      return translate(failure.removed ? "agentReleases.publishRules.removed" : "agentReleases.publishRules.notRemoved", {
        reason: translate("agentReleases.publishRules.mismatch"),
      });
    case "signature":
      return translate(failure.removed ? "agentReleases.publishRules.removed" : "agentReleases.publishRules.notRemoved", {
        reason: translate("agentReleases.publishRules.signatureFailed", { reason: describeSubmitError(failure.cause) }),
      });
  }
}

function failed(failure: PublishFailure): { failure: PublishFailure; error: string } {
  return { failure, error: failureText(failure) };
}

/** The four calls publishing makes. `ApiClient` satisfies it. */
export interface PublishClient {
  createAgentRelease(body: AgentReleaseCreate): Promise<AgentRelease>;
  uploadAgentReleaseArtifact(releaseId: string, file: Blob): Promise<AgentRelease>;
  setAgentReleaseSignature(releaseId: string, signature: string): Promise<void>;
  deleteAgentRelease(releaseId: string): Promise<void>;
}

/**
 * Publish each file in turn: create the row, upload the bytes, then -- only
 * if the digests agree -- record the release signature.
 *
 * ONE FILE AT A TIME, AND A FAILURE STOPS ONLY THAT FILE. Six uploads of tens
 * of MB in parallel would compete for one uplink and make every progress line
 * lie; and a 409 on Windows is no reason to leave Linux unpublished.
 *
 * `signed` IS ALWAYS FALSE. It is an Authenticode / Developer ID claim
 * (`ReleaseOut.signed`), and the release workflow does not code-sign. The
 * release signature is a different thing and has its own call.
 *
 * AN ENTRY WHOSE UPLOAD FAILED IS REMOVED. It was created a moment ago by this
 * run and has no bytes, so nothing can reference it -- and left in place, it
 * makes the obvious retry a 409 that only a `curl DELETE` could clear.
 */
export async function runPublish(
  api: PublishClient,
  version: string,
  items: PublishItem[],
  signatures: SignatureLine[] | null,
  onProgress: (outcomes: PublishOutcome[]) => void,
): Promise<PublishOutcome[]> {
  const outcomes: PublishOutcome[] = items.map(() => ({
    step: "waiting",
    created: false,
    uploaded: false,
    signed: false,
  }));
  const update = (index: number, patch: Partial<PublishOutcome>) => {
    outcomes[index] = { ...outcomes[index]!, ...patch };
    onProgress(outcomes.map((o) => ({ ...o })));
  };

  for (const [index, item] of items.entries()) {
    update(index, { step: "creating" });
    let release: AgentRelease;
    try {
      release = await api.createAgentRelease({
        version,
        os: item.os,
        arch: item.arch,
        signed: false,
        notes: "",
      });
    } catch (error) {
      update(index, { step: "failed", ...failed({ kind: "create", cause: error }) });
      continue;
    }

    update(index, { step: "uploading", created: true });
    let stored: AgentRelease;
    try {
      stored = await api.uploadAgentReleaseArtifact(release.id, item.file);
    } catch (error) {
      try {
        await api.deleteAgentRelease(release.id);
        update(index, {
          step: "failed",
          created: false,
          ...failed({ kind: "upload", cause: error, removed: true }),
        });
      } catch {
        update(index, {
          step: "failed",
          ...failed({ kind: "upload", cause: error, removed: false }),
        });
      }
      continue;
    }

    const decision = decideSignature(signatures, item.os, item.arch, stored.sha256);
    if (decision.kind === "mismatch") {
      // REMOVED, not left behind. The catalogue is unique on version, OS and
      // architecture and this page offers no way to replace a file, so an
      // entry left here would turn the operator's corrected retry into a 409
      // and send them to curl.
      update(index, {
        step: "failed",
        uploaded: true,
        sha256: stored.sha256,
        signature: decision.kind,
        ...(await removeEntry(api, release.id, (removed) => ({ kind: "mismatch", removed }))),
      });
      continue;
    }
    if (decision.kind !== "sign") {
      update(index, { step: "done", uploaded: true, sha256: stored.sha256, signature: decision.kind });
      continue;
    }

    update(index, { step: "signing", uploaded: true, sha256: stored.sha256 });
    try {
      await api.setAgentReleaseSignature(release.id, decision.signature);
      update(index, { step: "done", signed: true, signature: "sign" });
    } catch (error) {
      update(index, {
        step: "failed",
        ...(await removeEntry(api, release.id, (removed) => ({ kind: "signature", cause: error, removed }))),
      });
    }
  }
  return outcomes;
}

/**
 * Delete an entry this run created and could not finish, so publishing the
 * same file again is not refused as a duplicate. Says which happened.
 */
async function removeEntry(
  api: PublishClient,
  releaseId: string,
  failure: (removed: boolean) => PublishFailure,
): Promise<{ created: boolean; error: string; failure: PublishFailure }> {
  try {
    await api.deleteAgentRelease(releaseId);
    return { created: false, ...failed(failure(true)) };
  } catch {
    return { created: true, ...failed(failure(false)) };
  }
}
