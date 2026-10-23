import { describeSubmitError } from "../../components/form";
import { Button, Dialog } from "../../components/ui";
import { useT } from "../../i18n/context";
// The docs module's sanitizing renderer: article bodies are Markdown, and raw
// HTML in them never reaches the page.
import { renderMarkdown } from "../docs/markdown";
import { usePortalHelpArticle, usePortalHelpSuggestions } from "./api";

/**
 * Help articles that match the subject being typed. Shown only when there
 * is something to show; a failure is silent because the form comes first.
 */
export function HelpSuggestions({
  subject,
  enabled,
  onOpen,
}: {
  subject: string;
  enabled: boolean;
  onOpen: (articleId: string) => void;
}) {
  const t = useT();
  const suggestions = usePortalHelpSuggestions(subject, enabled);
  const items = suggestions.data ?? [];
  if (items.length === 0) return null;
  return (
    <section
      aria-label={t("portal.help.suggestions")}
      className="notice"
      style={{ marginBottom: 12 }}
    >
      <div className="cell-title">{t("portal.help.suggestions")}</div>
      <ul style={{ margin: "6px 0", paddingLeft: 18 }}>
        {items.map((article) => (
          <li key={article.id}>
            <Button variant="ghost" onClick={() => onOpen(article.id)}>
              {article.title}
            </Button>
          </li>
        ))}
      </ul>
      <p className="form-hint" style={{ margin: 0 }}>
        {t("portal.help.carryOn")}
      </p>
    </section>
  );
}

/** One help article, read without leaving the form. Keep it outside the
 * `<form>`: the dialog's close button is not a submit control. */
export function HelpArticleDialog({
  articleId,
  onClose,
}: {
  articleId: string | null;
  onClose: () => void;
}) {
  const t = useT();
  const article = usePortalHelpArticle(articleId);
  return (
    <Dialog open={articleId != null} onClose={onClose} title={article.data?.title ?? t("portal.help.articleTitle")}>
      {article.isError ? (
        <p role="alert" className="form-error">
          {t("portal.help.openFailed", { reason: describeSubmitError(article.error) })}
        </p>
      ) : article.data ? (
        <div
          className="overflow-x-auto"
          dangerouslySetInnerHTML={{ __html: renderMarkdown(article.data.body) }}
        />
      ) : (
        <p className="note">{t("portal.help.loading")}</p>
      )}
      <div className="flex justify-end" style={{ marginTop: 16 }}>
        <Button onClick={onClose}>{t("portal.help.back")}</Button>
      </div>
    </Dialog>
  );
}
