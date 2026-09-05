from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conference_scout.db.models import User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create_by_telegram_id(self, telegram_id: int) -> User:
        user = await self.session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            user = User(telegram_id=telegram_id)
            self.session.add(user)
            await self.session.flush()
        return user

    async def list_all(self) -> list[User]:
        return list(await self.session.scalars(select(User).order_by(User.id)))
