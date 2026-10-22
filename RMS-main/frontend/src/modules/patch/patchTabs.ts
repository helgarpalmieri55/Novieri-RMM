/**
 * THREE ECOSYSTEMS, THREE MODELS, ONE PAGE.
 *
 * THE OPERATOR'S DECISION, 2026-08-26: split patch management into Linux,
 * Windows (including Windows Server) and macOS, "because the one model the page
 * uses does not fit all three ecosystems". That is not a cosmetic split. The
 * three agent scanners disagree about what an update even IS, and the console
 * was averaging over the disagreement:
 *
 *   WINDOWS  `winupdate.go` reads `IUpdate.MsrcSeverity` and maps an unrated
 *            update to `unknown` -- and its own comment records that "most
 *            cumulative updates for Windows 11 carry no MSRC severity at all
 *            despite containing security fixes". An update is a KB article, a
 *            discrete thing, and WSUS's approve-for-a-computer-group model is
 *            exactly what `patch_approvals (org_id, patch_id, ring_name)` is.
 *
 *   LINUX    `apt.go` marks a version `important` if and only if its origin
 *            string contains "security" -- i.e. it came from the security
 *            pocket, which is Debian and Ubuntu's OWN definition of a security
 *            update and the only severity apt can honestly support. Everything
 *            else is `unknown`. And apt resolves dependencies as a SET, so
 *            approving one package out of 300 is frequently not actionable.
 *
 *   macOS    `mac.go` gets `Recommended: YES|NO` from `softwareupdate --list`
 *            and NO severity at all; recommended maps to `important` and the
 *            rest to `unknown`. So on this platform the severity column has two
 *            values and neither of them is a severity.
 *
 * WHAT FOLLOWS FROM THAT, and it is why this file exists rather than a
 * `switch` in a component: THE DEFAULT SEVERITY FILTER IS DIFFERENT ON EVERY
 * TAB, and each default is derived from the scanner above it.
 * `docs/modules/patch/view-redesign.md` §2 already saw this coming -- "on a
 * Windows-only fleet the right default may be 'all'" -- and could not act on it
 * while one page served all three fleets.
 */

import { translate } from "../../i18n/core";
import { lazyLabels } from "../../i18n/lazy";

/** `Patch.os_family` is `windows|linux|macos` and nothing else; the agent's
 *  `PatchScanDocument` constrains it to those three (spec §5). The ORDER here
 *  is the tab order and it matches the `Literal` in
 *  `backend/app/modules/patch/schema.py`, so the strip and the type read the
 *  same way round. */
export type PatchFamily = "windows" | "linux" | "macos";

export const PATCH_FAMILIES = ["windows", "linux", "macos"] as const satisfies readonly PatchFamily[];

/**
 * The tab an `?os=` value selects.
 *
 * WINDOWS IS THE DEFAULT AND IT IS A CHOICE, not an accident of ordering. A
 * default has to be STABLE -- "the tab with the most outstanding work" would
 * move under the operator between two visits and could not be linked to -- so
 * it is a constant, and the cost of picking wrong is paid off by the count on
 * every tab: a Windows-quiet, Linux-noisy fleet says "Linux 300" in the strip
 * before the operator has read a single row.
 */
export function patchFamilyFromParam(value: string | null): PatchFamily {
  return (PATCH_FAMILIES as readonly string[]).includes(value ?? "")
    ? (value as PatchFamily)
    : "windows";
}

/** The name a person uses. "Windows" covers Windows Server: `os_family` is
 *  `windows` for both, so they are one fleet to every query on this page and
 *  pretending otherwise in the strip would promise a filter that does not
 *  exist. */
export const FAMILY_LABEL: Record<PatchFamily, string> = {
  windows: "Windows",
  linux: "Linux",
  macos: "macOS",
};

/**
 * THE DEFAULT SEVERITY FILTER, PER PLATFORM, EACH DERIVED FROM ITS SCANNER.
 *
 * LINUX narrows to critical + important, because on apt `important` means
 * "from the security pocket" exactly, so the narrowed list IS the security
 * list -- and Linux is the platform that actually produces the 300-row page
 * the whole rollup rebuild exists for.
 *
 * WINDOWS does NOT narrow. Microsoft leaves most Windows 11 cumulative updates
 * unrated, so `critical,important` on this tab hides the monthly rollup --
 * `winupdate.go` says so in its own comment. A default that hides the one
 * update everybody is looking for is worse than a long list.
 *
 * macOS does not narrow either, and for a stronger reason: Apple publishes no
 * severity, so filtering by one is filtering by a field this platform does not
 * have. `important` here means "Apple recommends it", which is worth reading as
 * a column and is not worth defaulting a filter to.
 *
 * All three are one click from the other, the count of what is excluded is
 * always on the page, and the device panel is never severity-filtered.
 */
export function defaultSeveritiesFor(family: PatchFamily): string[] {
  return family === "linux" ? ["critical", "important"] : [];
}

/** What the severity column MEANS on this platform, said beside the filter that
 *  reads it. Three different sentences because three different things are true,
 *  and a reader who does not know which one applies cannot tell a narrow list
 *  from a short one. */
export const SEVERITY_PROVENANCE: Readonly<Record<PatchFamily, string>> = lazyLabels<PatchFamily>({
  windows: "patch.provenance.windows",
  linux: "patch.provenance.linux",
  macos: "patch.provenance.macos",
});

/** "3 Windows machines" / "1 macOS machine". */
export function familyMachineCount(family: PatchFamily, count: number): string {
  return translate("patch.family.machines", { count, label: FAMILY_LABEL[family] });
}
