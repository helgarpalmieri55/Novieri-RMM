"""Help articles for requesters: the portal-visible, published slice of the
documentation library.

Runs on the caller's `portal_session`. Migration 0160's restrictive policy
already limits `portal_user` to this org's flagged, unarchived articles and
its column grant to the columns selected here; the filters below repeat that
so the query means the same thing if it ever runs under another role.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import tr
from app.models import DocArticle
from app.modules.documentation.service import suggest_match
from app.modules.portal.errors import PortalNotFoundError
from app.modules.portal.schema import PortalHelpArticle, PortalHelpSuggestion

_PUBLISHED_FOR_PORTAL = (DocArticle.portal_visible.is_(True), DocArticle.archived.is_(False))


async def suggest_help(
    session: AsyncSession, *, org_id: UUID, text: str, limit: int
) -> list[PortalHelpSuggestion]:
    match = suggest_match(text)
    if match is None:
        return []
    matches, rank = match
    rows = (
        await session.execute(
            select(DocArticle.id, DocArticle.title)
            .where(DocArticle.org_id == org_id, *_PUBLISHED_FOR_PORTAL, matches)
            .order_by(rank.desc(), DocArticle.title, DocArticle.id)
            .limit(limit)
        )
    ).all()
    return [PortalHelpSuggestion(id=r.id, title=r.title) for r in rows]


async def get_help_article(
    session: AsyncSession, *, org_id: UUID, article_id: UUID
) -> PortalHelpArticle:
    """Raises `PortalNotFoundError` for an internal, archived, foreign or
    absent article alike, so the portal confirms nothing about ids it cannot
    show."""
    row = (
        await session.execute(
            select(DocArticle.id, DocArticle.title, DocArticle.body).where(
                DocArticle.org_id == org_id, DocArticle.id == article_id, *_PUBLISHED_FOR_PORTAL
            )
        )
    ).one_or_none()
    if row is None:
        raise PortalNotFoundError(tr("article {article_id} not found", article_id=article_id))
    return PortalHelpArticle(id=row.id, title=row.title, body=row.body)
