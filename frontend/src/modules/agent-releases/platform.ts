/**
 * An agent package's platform as an operator names it. The Go target
 * (`darwin/arm64`) stays available as secondary text, because it is what the
 * file on disk is called.
 */

import { translate, type MessageKey } from "../../i18n/core";

// The words are catalogue keys, looked up when asked for: a stored string would
// keep the language this module loaded in.
const OS: Record<string, MessageKey> = {
  windows: "agentReleases.platform.windows",
  linux: "agentReleases.platform.linux",
  darwin: "agentReleases.platform.darwin",
};

const ARCH: Record<string, Record<string, MessageKey>> = {
  darwin: { arm64: "agentReleases.platform.darwinArm64", amd64: "agentReleases.platform.darwinAmd64" },
  default: {
    amd64: "agentReleases.platform.amd64",
    arm64: "agentReleases.platform.arm64",
    "386": "agentReleases.platform.x386",
    arm: "agentReleases.platform.arm",
  },
};

/** The processor half alone: arm64 on a Mac is Apple silicon, elsewhere 64-bit ARM. */
export function archLabel(os: string, arch: string): string {
  const key = (ARCH[os] ?? ARCH.default!)[arch] ?? ARCH.default![arch];
  return key ? translate(key) : arch;
}

/** The operating system alone, as an operator names it ("macOS", not "darwin"). */
export function osLabel(os: string): string {
  const key = OS[os];
  return key ? translate(key) : os;
}

export function platformLabel(os: string, arch: string): string {
  return `${osLabel(os)} (${archLabel(os, arch)})`;
}

/**
 * What has not been tested on a platform, in words an operator can act on.
 *
 * `agent/packaging/README.md`, "What is untested": the Windows service and the
 * macOS daemon are cross-compiled and signed in CI but have never been
 * installed and run on a real machine. Linux is the path CI exercises. This
 * wording comes off only by the procedure in
 * `docs/modules/agent-distribution/first-run-verification.md` part D.
 */
const UNTESTED: Record<string, MessageKey> = {
  windows: "agentReleases.platform.untestedWindows",
  darwin: "agentReleases.platform.untestedDarwin",
};

export function untestedCaveat(os: string): string | null {
  const key = UNTESTED[os];
  return key ? translate(key) : null;
}
