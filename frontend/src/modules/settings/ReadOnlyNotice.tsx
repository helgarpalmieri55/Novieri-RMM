// Why a tab's fields are read-only, said ABOVE them (redesign, decision D22).
//
// The reason used to sit beside a disabled "Save changes" button at the foot
// of each tab. The kit's `SaveBar` is drawn only while there is a change, and
// a person who cannot edit can make none, so there is no bar to say it in: the
// sentence moves to a notice at the top of the tab, where it is read before
// anyone tries a field. It is the same sentence (`useGlobalWriteReason`), and
// it is courtesy, not security: the server refuses the write regardless
// (invariant 6).

import { KIND_ICON } from "../../shell/components";
import { Icon } from "../../shell/icons";

export function ReadOnlyNotice({ reason }: { reason: string }) {
  return (
    <p className="notice" style={{ marginBottom: 14 }}>
      <Icon name={KIND_ICON.info} size={16} />
      <span>{reason}</span>
    </p>
  );
}
