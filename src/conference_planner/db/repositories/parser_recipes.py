from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conference_planner.db.models import SiteParserRecipe
from conference_planner.schemas.parser_recipes import (
    PageKind,
    ParserRecipe,
    RecipeStatus,
)
from conference_planner.web.recipes import RecipeValidationError, validate_recipe


class ParserRecipeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def active_for(self, url: str, page_kind: PageKind) -> SiteParserRecipe | None:
        domain = (urlsplit(url).hostname or "").casefold().rstrip(".")
        return await self.session.scalar(
            select(SiteParserRecipe).where(
                SiteParserRecipe.domain == domain,
                SiteParserRecipe.page_kind == page_kind,
                SiteParserRecipe.status == RecipeStatus.ACTIVE,
            )
        )

    async def propose(self, recipe: ParserRecipe) -> SiteParserRecipe:
        try:
            validate_recipe(recipe)
        except RecipeValidationError as exc:
            raise ValueError(str(exc)) from exc
        latest = await self.session.scalar(
            select(func.max(SiteParserRecipe.version)).where(
                SiteParserRecipe.domain == recipe.domain,
                SiteParserRecipe.page_kind == recipe.page_kind,
            )
        )
        stored = SiteParserRecipe(
            domain=recipe.domain,
            page_kind=recipe.page_kind,
            version=(latest or 0) + 1,
            status=RecipeStatus.PROPOSED,
            card_selector=recipe.card_selector,
            field_selectors=recipe.field_selectors,
            link_labels=list(recipe.link_labels),
            regex_patterns=recipe.regex_patterns,
        )
        self.session.add(stored)
        await self.session.flush()
        return stored

    async def activate_after_validation(self, recipe_id: int) -> SiteParserRecipe:
        stored = await self._require(recipe_id)
        validate_recipe(self._schema(stored))
        stored.status = RecipeStatus.VALIDATED
        active = list(
            await self.session.scalars(
                select(SiteParserRecipe).where(
                    SiteParserRecipe.domain == stored.domain,
                    SiteParserRecipe.page_kind == stored.page_kind,
                    SiteParserRecipe.status == RecipeStatus.ACTIVE,
                    SiteParserRecipe.id != stored.id,
                )
            )
        )
        for previous in active:
            previous.status = RecipeStatus.DISABLED
        stored.status = RecipeStatus.ACTIVE
        stored.failure_count = 0
        stored.last_error_code = None
        await self.session.flush()
        return stored

    async def mark_degraded(
        self, recipe_id: int, error_code: str
    ) -> SiteParserRecipe:
        stored = await self._require(recipe_id)
        stored.status = RecipeStatus.DEGRADED
        stored.failure_count += 1
        stored.last_error_code = error_code[:100]
        await self.session.flush()
        return stored

    @staticmethod
    def to_schema(stored: SiteParserRecipe) -> ParserRecipe:
        return ParserRecipeRepository._schema(stored)

    async def _require(self, recipe_id: int) -> SiteParserRecipe:
        stored = await self.session.get(SiteParserRecipe, recipe_id)
        if stored is None:
            raise ValueError("parser recipe not found")
        return stored

    @staticmethod
    def _schema(stored: SiteParserRecipe) -> ParserRecipe:
        return ParserRecipe(
            domain=stored.domain,
            page_kind=stored.page_kind,
            card_selector=stored.card_selector,
            field_selectors=stored.field_selectors,
            link_labels=tuple(stored.link_labels),
            regex_patterns=stored.regex_patterns,
        )
