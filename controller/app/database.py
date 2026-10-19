import logging
from typing import Generator
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session
from controller.app.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


# SQLite fallback is allowed for unit tests, while PostgreSQL is used for staging/production
connect_args = {}
if settings.DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10 if not settings.DATABASE_URL.startswith("sqlite") else 5,
    max_overflow=20 if not settings.DATABASE_URL.startswith("sqlite") else 0,
    connect_args=connect_args,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

