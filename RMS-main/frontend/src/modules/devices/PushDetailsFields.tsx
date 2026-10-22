/**
 * The three facts a push needs about a machine that has never run the agent,
 * as form fields: its DNS name, its operating system and its processor.
 *
 * WHY THEY ARE TYPED BY A PERSON. On a managed device inventory reports all
 * three, and the server refuses a hand edit there (they are machine-owned). A
 * machine a push is FOR has never reported anything, and push refuses without
 * them:
 *
 *   - the DNS NAME, because Windows push is Kerberos only and a ticket is
 *     issued for a name, never an address (design 2026-09-28 §2.3). It is never
 *     looked up from reverse DNS (plan W10): that would let whoever answers PTR
 *     queries choose which service a domain credential is presented to.
 *   - the OS and PROCESSOR, because push picks the package from them (spec
 *     §7.2). The agent checks both on the target before installing, so a wrong
 *     one fails cleanly rather than installing the wrong binary.
 *
 * Shared by "Add asset" and "Push install details" so the two forms cannot
 * describe the same field differently.
 */

import { useT } from "../../i18n/context";
import { translate } from "../../i18n/core";
import { Field, inputClass } from "../../components/ui";
import type { Device } from "../../lib/api";

/** Read when drawn, so it follows the language. */
export const dnsNameHint = (): string => translate("devices.pushDetails.dnsHint");

export const OS_OPTIONS: { value: NonNullable<Device["os_family"]>; label: string }[] = [
  { value: "windows", label: "Windows" },
  { value: "linux", label: "Linux" },
  { value: "macos", label: "macOS" },
];

/**
 * `label` is a getter: a stored string would be the output of a lookup frozen
 * in the language active when this module loaded.
 */
export const ARCH_OPTIONS: { value: "amd64" | "arm64"; readonly label: string }[] = [
  {
    value: "amd64",
    get label() {
      return translate("devices.pushDetails.amd64");
    },
  },
  {
    value: "arm64",
    get label() {
      return translate("devices.pushDetails.arm64");
    },
  },
];

export interface PushDetails {
  fqdn: string;
  os: "" | NonNullable<Device["os_family"]>;
  arch: "" | "amd64" | "arm64";
}

export function PushDetailsFields({
  value,
  onChange,
}: {
  value: PushDetails;
  onChange: (next: PushDetails) => void;
}) {
  const t = useT();
  return (
    <>
      <Field label={t("devices.pushDetails.dnsName")} help={dnsNameHint()}>
        <input
          className={inputClass}
          value={value.fqdn}
          onChange={(event) => onChange({ ...value, fqdn: event.target.value })}
          placeholder={t("devices.pushDetails.dnsPlaceholder")}
          autoComplete="off"
          spellCheck={false}
        />
      </Field>
      <Field label={t("devices.pushDetails.os")}>
        <select
          className={inputClass}
          value={value.os}
          onChange={(event) => onChange({ ...value, os: event.target.value as PushDetails["os"] })}
        >
          <option value="">{t("devices.pushDetails.notRecorded")}</option>
          {OS_OPTIONS.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
      </Field>
      <Field label={t("devices.pushDetails.processor")}>
        <select
          className={inputClass}
          value={value.arch}
          onChange={(event) => onChange({ ...value, arch: event.target.value as PushDetails["arch"] })}
        >
          <option value="">{t("devices.pushDetails.notRecorded")}</option>
          {ARCH_OPTIONS.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
      </Field>
    </>
  );
}
