/**
 * The pure half of "Publish a release": what a file is, what SIGNATURES says,
 * and -- the part that matters -- when a signature may be sent.
 *
 * The control plane does not verify a release signature (it cannot; it does
 * not hold the key), and the agent checks it only at self-update time, in the
 * field. So the console's digest comparison is the one place a signature
 * attached to the wrong bytes is caught before it reaches the catalogue.
 */

import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../../lib/api";
import type { AgentRelease } from "../../lib/api";
import {
  MAX_ARTIFACT_BYTES,
  type PublishClient,
  type PublishItem,
  type PublishOutcome,
  decideSignature,
  inferPlatform,
  isStrictSemver,
  parseSignatures,
  planProblems,
  runPublish,
} from "./publish";

const SHA_A = "a".repeat(64);
const SHA_B = "b".repeat(64);
const SIG = `${"A".repeat(86)}==`;
const SIG2 = `${"B".repeat(86)}==`;

describe("isStrictSemver", () => {
  it("accepts what the release workflow accepts", () => {
    for (const v of ["1.4.0", "0.0.1", "10.20.30", "1.4.0-rc1", "1.4.0-rc.1", "2.0.0-alpha-2.x"]) {
      expect(isStrictSemver(v)).toBe(true);
    }
  });

  it("refuses what the workflow refuses", () => {
    for (const v of ["v1.4.0", "1.4", "01.4.0", "1.4.0+build", "1.4.0-", "873d666", "dev", " 1.4.0", ""]) {
      expect(isStrictSemver(v)).toBe(false);
    }
  });
});

describe("inferPlatform", () => {
  it("reads the names build.sh gives the binaries", () => {
    expect(inferPlatform("its-agent-linux-amd64")).toEqual({ os: "linux", arch: "amd64" });
    expect(inferPlatform("its-agent-darwin-arm64")).toEqual({ os: "darwin", arch: "arm64" });
    expect(inferPlatform("its-agent-windows-amd64.exe")).toEqual({ os: "windows", arch: "amd64" });
    expect(inferPlatform("its-agent-windows-arm64.exe")).toEqual({ os: "windows", arch: "arm64" });
  });

  it("does not guess at a name it does not recognise", () => {
    // A browser's "(1)" on a second download, an alias the server would
    // refuse, and the side files of the release are all left to the operator.
    for (const name of [
      "its-agent-linux-amd64 (1)",
      "its-agent-linux-x86_64",
      "its-agent-freebsd-amd64",
      "agent-linux-amd64",
      "SHA256SUMS",
      "release-signing-key.pub",
      "its-agent-linux-amd64.sig",
    ]) {
      expect(inferPlatform(name)).toBeNull();
    }
  });
});

describe("parseSignatures", () => {
  it("reads the lines release-sign writes", () => {
    const text =
      `${SHA_A}  its-agent-linux-amd64  linux/amd64  ${SIG}\n` +
      `${SHA_B}  its-agent-windows-amd64.exe  windows/amd64  ${SIG2}\n`;
    const parsed = parseSignatures(text);
    expect(parsed.problems).toEqual([]);
    expect(parsed.entries).toEqual([
      { sha256: SHA_A, filename: "its-agent-linux-amd64", os: "linux", arch: "amd64", signature: SIG },
      { sha256: SHA_B, filename: "its-agent-windows-amd64.exe", os: "windows", arch: "amd64", signature: SIG2 },
    ]);
  });

  it("tolerates Windows line endings and blank lines", () => {
    const parsed = parseSignatures(`\r\n${SHA_A}  its-agent-linux-amd64  linux/amd64  ${SIG}\r\n\r\n`);
    expect(parsed.problems).toEqual([]);
    expect(parsed.entries).toHaveLength(1);
  });

  it("names the lines it cannot read rather than dropping them silently", () => {
    const text =
      `${SHA_A}  its-agent-linux-amd64  linux/amd64  ${SIG}\n` +
      `not a signature line\n` +
      `${SHA_B}  its-agent-linux-arm64  linux/arm64  short==\n`;
    const parsed = parseSignatures(text);
    expect(parsed.entries).toHaveLength(1);
    expect(parsed.problems).toEqual([
      "Line 2 of the signatures file is not in the expected form.",
      "Line 3 of the signatures file is not in the expected form.",
    ]);
  });

  it("refuses to choose between two lines for one platform", () => {
    const text =
      `${SHA_A}  its-agent-linux-amd64  linux/amd64  ${SIG}\n` +
      `${SHA_B}  its-agent-linux-amd64  linux/amd64  ${SIG2}\n`;
    const parsed = parseSignatures(text);
    expect(parsed.entries).toHaveLength(0);
    expect(parsed.problems).toEqual([
      "The signatures file has more than one line for Linux (64-bit Intel/AMD).",
    ]);
  });
});

describe("decideSignature", () => {
  const entries = parseSignatures(`${SHA_A}  its-agent-linux-amd64  linux/amd64  ${SIG}\n`).entries;

  it("signs when the platform matches and the uploaded digest is the signed one", () => {
    expect(decideSignature(entries, "linux", "amd64", SHA_A)).toEqual({ kind: "sign", signature: SIG });
    // The server's hex and the tool's hex are both lowercase today; the
    // comparison should not depend on it.
    expect(decideSignature(entries, "linux", "amd64", SHA_A.toUpperCase())).toEqual({
      kind: "sign",
      signature: SIG,
    });
  });

  it("refuses to sign bytes that are not the ones the signature covers", () => {
    expect(decideSignature(entries, "linux", "amd64", SHA_B)).toEqual({
      kind: "mismatch",
      expected: SHA_A,
      actual: SHA_B,
    });
  });

  it("says when there is nothing to sign with", () => {
    expect(decideSignature(entries, "linux", "arm64", SHA_A)).toEqual({ kind: "no-line" });
    expect(decideSignature(null, "linux", "amd64", SHA_A)).toEqual({ kind: "no-file" });
  });
});

function file(name: string, size = 10): File {
  return new File([new Uint8Array(size)], name);
}

describe("planProblems", () => {
  it("wants a version, a file, and a platform for every file", () => {
    expect(planProblems("", [])).toEqual([
      "Enter the version the release workflow built.",
      "Choose at least one agent binary.",
    ]);
    expect(planProblems("1.4", [{ file: file("x"), os: "", arch: "" }])).toEqual([
      "The version must be a plain release number such as 1.4.0, with no leading v.",
      "Choose the platform for x.",
    ]);
  });

  it("will not publish two files as the same platform", () => {
    const items: PublishItem[] = [
      { file: file("its-agent-linux-amd64"), os: "linux", arch: "amd64" },
      { file: file("copy"), os: "linux", arch: "amd64" },
    ];
    expect(planProblems("1.4.0", items)).toEqual([
      "Two files are marked Linux (64-bit Intel/AMD). Each platform can be published once per version.",
    ]);
  });

  it("refuses a file larger than the server accepts before sending it", () => {
    const big = { size: MAX_ARTIFACT_BYTES + 1, name: "its-agent-linux-amd64" } as File;
    expect(planProblems("1.4.0", [{ file: big, os: "linux", arch: "amd64" }])).toEqual([
      "its-agent-linux-amd64 is larger than the 128 MiB the server accepts.",
    ]);
  });
});

function row(os: string, arch: string, sha256: string): AgentRelease {
  return {
    id: `id-${os}-${arch}`,
    version: "1.4.0",
    os,
    arch,
    size_bytes: 10,
    sha256,
    signed: false,
    notes: "",
    published_at: null,
    has_artifact: sha256 !== "",
  };
}

function client(overrides: Partial<PublishClient> = {}): PublishClient & {
  [K in keyof PublishClient]: ReturnType<typeof vi.fn>;
} {
  return {
    createAgentRelease: vi.fn((body: { os: string; arch: string }) =>
      Promise.resolve(row(body.os, body.arch, "")),
    ),
    uploadAgentReleaseArtifact: vi.fn((id: string) => {
      const [, os, arch] = id.split("-");
      return Promise.resolve(row(os!, arch!, SHA_A));
    }),
    setAgentReleaseSignature: vi.fn(() => Promise.resolve()),
    deleteAgentRelease: vi.fn(() => Promise.resolve()),
    ...overrides,
  } as never;
}

describe("runPublish", () => {
  const linux: PublishItem = { file: file("its-agent-linux-amd64"), os: "linux", arch: "amd64" };
  const arm: PublishItem = { file: file("its-agent-linux-arm64"), os: "linux", arch: "arm64" };

  it("creates, uploads, then records the signature -- and claims no code signing", async () => {
    const api = client();
    const signatures = parseSignatures(`${SHA_A}  its-agent-linux-amd64  linux/amd64  ${SIG}\n`).entries;
    const outcomes = await runPublish(api, "1.4.0", [linux], signatures, () => {});

    expect(api.createAgentRelease).toHaveBeenCalledWith({
      version: "1.4.0",
      os: "linux",
      arch: "amd64",
      signed: false,
      notes: "",
    });
    expect(api.uploadAgentReleaseArtifact).toHaveBeenCalledWith("id-linux-amd64", linux.file);
    expect(api.setAgentReleaseSignature).toHaveBeenCalledWith("id-linux-amd64", SIG);
    expect(outcomes[0]).toMatchObject({ step: "done", created: true, uploaded: true, signed: true });
  });

  it("does not send a signature for bytes whose digest differs, and says so", async () => {
    const api = client();
    const signatures = parseSignatures(`${SHA_B}  its-agent-linux-amd64  linux/amd64  ${SIG}\n`).entries;
    const outcomes = await runPublish(api, "1.4.0", [linux], signatures, () => {});

    expect(api.setAgentReleaseSignature).not.toHaveBeenCalled();
    // Removed, so publishing the right file again is not a 409 that only curl
    // could clear.
    expect(api.deleteAgentRelease).toHaveBeenCalledWith("id-linux-amd64");
    expect(outcomes[0]).toMatchObject({ step: "failed", created: false, signed: false });
    expect(outcomes[0]!.error).toMatch(/not the file the signatures file was written for/i);
    expect(outcomes[0]!.error).toMatch(/publish this file again/i);
  });

  it("says so when a mismatched entry could not be removed", async () => {
    const api = client({ deleteAgentRelease: vi.fn(() => Promise.reject(new ApiError(503, ""))) });
    const signatures = parseSignatures(`${SHA_B}  its-agent-linux-amd64  linux/amd64  ${SIG}\n`).entries;
    const outcomes = await runPublish(api, "1.4.0", [linux], signatures, () => {});

    expect(outcomes[0]).toMatchObject({ step: "failed", created: true });
    expect(outcomes[0]!.error).toMatch(/could not be removed/i);
  });

  it("removes the entry when the signature cannot be recorded", async () => {
    const api = client({
      setAgentReleaseSignature: vi.fn(() => Promise.reject(new ApiError(503, ""))),
    });
    const signatures = parseSignatures(`${SHA_A}  its-agent-linux-amd64  linux/amd64  ${SIG}\n`).entries;
    const outcomes = await runPublish(api, "1.4.0", [linux], signatures, () => {});

    expect(api.deleteAgentRelease).toHaveBeenCalledWith("id-linux-amd64");
    expect(outcomes[0]).toMatchObject({ step: "failed", created: false, signed: false });
  });

  it("carries on after a refusal and reports the server's reason", async () => {
    const api = client({
      createAgentRelease: vi.fn((body: { os: string; arch: string }) =>
        body.arch === "amd64"
          ? Promise.reject(
              new ApiError(
                409,
                JSON.stringify({ detail: "a release already exists for this version, OS and architecture" }),
              ),
            )
          : Promise.resolve(row(body.os, body.arch, "")),
      ),
    });
    const outcomes = await runPublish(api, "1.4.0", [linux, arm], null, () => {});

    expect(outcomes[0]).toMatchObject({ step: "failed", created: false });
    expect(outcomes[0]!.error).toBe("a release already exists for this version, OS and architecture");
    expect(outcomes[1]).toMatchObject({ step: "done", uploaded: true, signed: false });
    expect(api.uploadAgentReleaseArtifact).toHaveBeenCalledTimes(1);
  });

  it("removes the entry it just created when the upload fails, so a retry is not a conflict", async () => {
    const api = client({
      uploadAgentReleaseArtifact: vi.fn(() => Promise.reject(new ApiError(503, ""))),
    });
    const outcomes = await runPublish(api, "1.4.0", [linux], null, () => {});

    expect(api.deleteAgentRelease).toHaveBeenCalledWith("id-linux-amd64");
    expect(outcomes[0]).toMatchObject({ step: "failed", created: false, uploaded: false });
    expect(outcomes[0]!.error).toMatch(/unavailable/i);
  });

  it("reports progress for each file as it goes", async () => {
    const api = client();
    const seen: PublishOutcome["step"][] = [];
    await runPublish(api, "1.4.0", [linux], null, (outcomes) => seen.push(outcomes[0]!.step));
    expect(seen).toEqual(["creating", "uploading", "done"]);
  });
});
