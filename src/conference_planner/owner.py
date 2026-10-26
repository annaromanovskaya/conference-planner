from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from conference_planner.db.repositories.users import UserRepository


@dataclass(frozen=True, slots=True)
class OwnerContext:
    user_id: int
    telegram_id: int


async def bootstrap_owner(session: AsyncSession, telegram_id: int) -> OwnerContext:
    user = await UserRepository(session).get_or_create_by_telegram_id(telegram_id)
    await session.commit()
    return OwnerContext(user_id=user.id, telegram_id=user.telegram_id)
