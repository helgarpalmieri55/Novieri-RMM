import { useMutation, useQueryClient } from "@tanstack/react-query";
import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import type { DocArticle } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { Toggle } from "../../shell/components";

/**
 * Whether people who raise tickets can read this article in the portal.
 * Off unless someone turns it on; the server checks docs.manage either way.
 */
export function PortalVisibility({ article, canManage }: { article: DocArticle; canManage: boolean }) {
  const t = useT();
  const api = useApi();
  const queryClient = useQueryClient();
  const visible = article.portal_visible ?? false;
  const save = useMutation({
    mutationFn: (next: boolean) => api.setArticlePortalVisibility(article.id, next),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["doc-article", article.id] });
      void queryClient.invalidateQueries({ queryKey: ["doc-articles"] });
    },
  });

  if (!canManage || article.archived) {
    return visible && !article.archived ? <p className="note mb-3">{t("docs.portal.visibleNote")}</p> : null;
  }
  const shown = save.isPending ? (save.variables ?? visible) : visible;
  return (
    <div className="mb-3">
      <Toggle
        label={t("docs.portal.toggle")}
        checked={shown}
        disabled={save.isPending}
        onChange={(next) => save.mutate(next)}
      />
      <p className="note">
        {t("docs.portal.explain")}
      </p>
      {save.isError && (
        <p role="alert" className="form-error">
          {t("docs.portal.notSaved", { reason: describeSubmitError(save.error) })}
        </p>
      )}
    </div>
  );
}
