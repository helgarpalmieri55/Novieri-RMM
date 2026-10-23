// The "this tab did not load" state, in one place across all the tabs.
//
// WHAT WAS WRONG WITH THE SIX COPIES THIS REPLACES. Every tab rendered the
// identical literal:
//
//     <p className="text-sm text-[var(--red)]">
//       That did not load. Retry, or check the platform status page.
//     </p>
//
// Two defects in one paragraph. It DISCARDED the error entirely -- a 503 from a
// proxy, a 500 from the app and a network drop all produced the same sentence,
// so the operator could not tell "wait and retry" from "this will never work
// until someone fixes the deployment". And it carried no `role`, so a screen
// reader announced nothing at all when a tab that was loading became a tab
// that had failed; the colour was the entire signal.
//
// So the reason is now said, through the one function that says reasons
// (cycle 4), and the paragraph is announced. It is drawn as the design's red
// notice with the critical glyph in front, so the colour is not the only
// carrier either. The lead-in stays because it names WHAT failed -- the read,
// not a save the operator just attempted, which is the distinction
// `describeSubmitError`'s sentences alone cannot draw.

import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import { KIND_ICON } from "../../shell/components";
import { Icon } from "../../shell/icons";

export function LoadFailure({ error }: { error: unknown }) {
  const t = useT();
  return (
    <p role="alert" className="notice red">
      <Icon name={KIND_ICON.crit} size={16} />
      <span>
        {t("settings.loadFailure", { reason: describeSubmitError(error) })}
      </span>
    </p>
  );
}
