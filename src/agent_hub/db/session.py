from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agent_hub.config import settings

engine = create_async_engine(settings.database_url, pool_size=10)
Session = async_sessionmaker(engine, expire_on_commit=False)
