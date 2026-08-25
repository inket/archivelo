import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(String(500))
    subscribed: Mapped[bool] = mapped_column(Boolean, default=False)
    subscribed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    last_checked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    last_check_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    last_check_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Video(Base):
    __tablename__ = "videos"
    __table_args__ = (UniqueConstraint("url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(String(500))
    thumbnail_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    published_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    published_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)

    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    category: Mapped["Category | None"] = relationship("Category")

    # discovered -> queued -> downloading -> downloaded / failed / skipped
    status: Mapped[str] = mapped_column(String(20), default="discovered")
    progress_percent: Mapped[float] = mapped_column(default=0.0)
    speed_label: Mapped[str | None] = mapped_column(String(50), nullable=True)
    eta_label: Mapped[str | None] = mapped_column(String(50), nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    watch_position_seconds: Mapped[float] = mapped_column(default=0.0)

    discovered_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow
    )
    queued_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
