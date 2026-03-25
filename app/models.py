from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Series(Base):
    __tablename__ = "series"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    albums: Mapped[list["Album"]] = relationship(back_populates="series", cascade="all, delete-orphan")


class Album(Base):
    __tablename__ = "albums"
    __table_args__ = (UniqueConstraint("series_id", "slug", name="uq_series_album_slug"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("series.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    slug: Mapped[str] = mapped_column(String(255), index=True)
    path: Mapped[str] = mapped_column(String(1024), unique=True)
    poster_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    series: Mapped[Series] = relationship(back_populates="albums")
    tracks: Mapped[list["Track"]] = relationship(back_populates="album", cascade="all, delete-orphan")


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[int] = mapped_column(primary_key=True)
    album_id: Mapped[int] = mapped_column(ForeignKey("albums.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    path: Mapped[str] = mapped_column(String(1024), unique=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    track_no: Mapped[int] = mapped_column(Integer, default=0)

    album: Mapped[Album] = relationship(back_populates="tracks")
