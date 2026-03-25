from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(f"sqlite:///{settings.db_path}", future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
