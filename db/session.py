# db/session.py
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from config import settings  # Предполагается, что pydantic-settings готов

# Строка подключения формата mysql+asyncmy://user:pass@host:port/dbname
DATABASE_URL = settings.db_url

engine = create_async_engine(
    DATABASE_URL,
    echo=True,  # Логировать SQL-запросы в консоль (полезно при разработке)
    pool_pre_ping=True
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)