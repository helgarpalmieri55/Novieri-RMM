import { NoValue } from "../../components/emptystate";
import { useT } from "../../i18n/context";
import { NO_DATE, formatDateTime } from "../../lib/datetime";
import { Panel, StatusPill } from "../../shell/components";
import { Facts } from "../../shell/viz";
import { useDeviceProvisioning, useOsImages, useProvisionProfiles, useUserTimezone } from "./api";
import { imagePickerOptions, nameForOption, profilePickerOptions } from "./pickerOptions";
import { provisionRunStateKind, provisionRunStateLabel } from "./provisioningState";

/** A single device's provisioning status, on the device detail page, as v3's `facts` strip. */
export function DeviceProvisioningTab({ deviceId }: { deviceId: string }) {
  const t = useT();
  const status = useDeviceProvisioning(deviceId);
  const zone = useUserTimezone();
  const data = status.data;

  // The profile and image were rendered as raw UUIDs: "the audit log
  // identifies every actor and target by raw UUID only; you cannot tell who
  // did what", the same defect on a device tab. Both lists are already cached
  // by the provisioning pages under their own keys, so this costs no extra
  // request when the operator has been on one; `nameForOption` falls back to
  // the id rather than to blank, so a device that HAS a profile never renders
  // as one that does not.
  const profiles = useProvisionProfiles();
  const images = useOsImages();
  const profileOptions = profilePickerOptions(profiles.data ?? [], images.data ?? []);
  const imageOptions = imagePickerOptions(images.data ?? []);

  return (
    <Panel title={t("provisioning.deviceTab.title")}>
      <Facts
        items={[
          {
            k: t("provisioning.deviceTab.latestRunState"),
            v: data?.latest_run_state ? (
              <StatusPill
                value={data.latest_run_state}
                kind={provisionRunStateKind(data.latest_run_state)}
                label={provisionRunStateLabel(data.latest_run_state)}
              />
            ) : (
              <NoValue label={t("provisioning.deviceTab.noRun")} />
            ),
          },
          { k: t("provisioning.deviceTab.profile"), v: nameForOption(profileOptions, data?.profile_id) ?? NO_DATE },
          { k: t("provisioning.deviceTab.image"), v: nameForOption(imageOptions, data?.image_id) ?? NO_DATE },
          // `formatDateTime` handles the absent case itself, returning
          // NO_DATE, the same dash the sibling fields print. The old ternary's
          // "not enrolled" was a fourth way of saying "nothing here" on a
          // screen that already had one.
          { k: t("provisioning.deviceTab.enrolled"), v: formatDateTime(data?.enrolled_at, zone) },
        ]}
      />
    </Panel>
  );
}
