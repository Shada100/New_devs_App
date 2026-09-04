from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool
import logging
from ..config import settings

logger = logging.getLogger(__name__)


def _async_database_url() -> str:
    """
    SQLAlchemy needs an explicit async driver. The configured URL uses the plain
    `postgresql://` scheme (that is what docker-compose passes in), which resolves
    to psycopg2 and blows up inside the async engine, so swap in asyncpg here.
    """
    url = settings.database_url
    if url.startswith("postgresql+"):
        return url
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


class DatabasePool:
    def __init__(self):
        self.engine = None
        self.session_factory = None

    async def initialize(self):
        """Initialize database connection pool"""
        if self.session_factory is not None:
            return

        try:
            database_url = _async_database_url()

            self.engine = create_async_engine(
                database_url,
                # The async engine needs the async-aware pool; plain QueuePool
                # is rejected outright at engine creation.
                poolclass=AsyncAdaptedQueuePool,
                pool_size=settings.database_pool_size,
                max_overflow=30,  # Additional connections when needed
                pool_pre_ping=True,  # Validate connections
                pool_recycle=settings.database_pool_recycle,
                echo=False,  # Set to True for SQL debugging
            )

            self.session_factory = async_sessionmaker(
                bind=self.engine,
                class_=AsyncSession,
                expire_on_commit=False,
            )

            logger.info("✅ Database connection pool initialized")

        except Exception as e:
            logger.error(f"❌ Database pool initialization failed: {e}")
            self.engine = None
            self.session_factory = None
            raise

    async def close(self):
        """Close database connections"""
        if self.engine:
            await self.engine.dispose()
        self.engine = None
        self.session_factory = None

    @asynccontextmanager
    async def get_session(self) -> AsyncIterator[AsyncSession]:
        """Get database session from pool"""
        if not self.session_factory:
            await self.initialize()
        if not self.session_factory:
            raise RuntimeError("Database pool not initialized")
        async with self.session_factory() as session:
            yield session


# Global database pool instance
db_pool = DatabasePool()


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Dependency to get database session"""
    async with db_pool.get_session() as session:
        yield session
