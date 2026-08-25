import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app import config

os.makedirs(os.path.dirname(config.DATABASE_PATH), exist_ok=True)

engine = create_engine(
    f"sqlite:///{config.DATABASE_PATH}",
    connect_args={"check_same_thread": False},
)

with engine.connect() as conn:
    conn.exec_driver_sql("PRAGMA journal_mode=WAL")

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def _add_missing_columns():
    """Minimal dev-time migration: add any columns that exist on the model
    but not yet on the table, so an existing sqlite file doesn't need to be
    deleted every time the schema gains a nullable column."""
    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table.name})")}
            for column in table.columns:
                if column.name not in existing:
                    ddl_type = column.type.compile(dialect=engine.dialect)
                    conn.exec_driver_sql(
                        f"ALTER TABLE {table.name} ADD COLUMN {column.name} {ddl_type}"
                    )
        conn.commit()


def _backfill_published_at():
    from app import scraper
    from app.models import Video

    session = SessionLocal()
    try:
        rows = (
            session.query(Video)
            .filter(Video.published_at.is_(None), Video.published_label.isnot(None))
            .all()
        )
        for video in rows:
            video.published_at = scraper.parse_relative_time(video.published_label)
        session.commit()
    finally:
        session.close()


def _backfill_video_categories():
    from app import matching
    from app.models import Category, Video

    session = SessionLocal()
    try:
        guess_from = matching.build_sorted_categories(session.query(Category).all())
        rows = session.query(Video).filter(Video.category_id.is_(None)).all()
        for video in rows:
            video.category_id = matching.guess_category_id(video.title, guess_from)
        session.commit()
    finally:
        session.close()


def init_db():
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
    _backfill_published_at()
    _backfill_video_categories()
