from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    places: Mapped[list["Place"]] = relationship(back_populates="creator")
    favorites: Mapped[list["Favorite"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    trip_stops: Mapped[list["TripStop"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Region(Base):
    """A traveler-facing hub, not an administrative city boundary."""

    __tablename__ = "regions"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    name_ko: Mapped[str] = mapped_column(String(100))
    name_local: Mapped[str] = mapped_column(String(100))
    island: Mapped[str] = mapped_column(String(80), index=True)
    kind: Mapped[str] = mapped_column(String(30), default="hub")
    center_lat: Mapped[float] = mapped_column(Float)
    center_lng: Mapped[float] = mapped_column(Float)
    default_zoom: Mapped[int] = mapped_column(Integer, default=12)
    south: Mapped[float] = mapped_column(Float)
    west: Mapped[float] = mapped_column(Float)
    north: Mapped[float] = mapped_column(Float)
    east: Mapped[float] = mapped_column(Float)
    summary: Mapped[str] = mapped_column(Text, default="")
    access_note: Mapped[str] = mapped_column(Text, default="")
    transport_mode: Mapped[str] = mapped_column(String(80), default="car")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    places: Mapped[list["Place"]] = relationship(back_populates="region")


class Place(Base):
    """A WGS84 place with Bali-specific trip-planning facts."""

    __tablename__ = "places"

    id: Mapped[int] = mapped_column(primary_key=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id", ondelete="RESTRICT"), index=True)
    creator_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    category: Mapped[str] = mapped_column(String(30), index=True)
    title: Mapped[str] = mapped_column(String(180))
    local_name: Mapped[str] = mapped_column(String(180), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    area: Mapped[str] = mapped_column(String(100), default="")
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=90)
    budget_level: Mapped[int] = mapped_column(Integer, default=1)
    best_time: Mapped[str] = mapped_column(String(120), default="")
    access_type: Mapped[str] = mapped_column(String(60), default="road")
    booking_required: Mapped[bool] = mapped_column(Boolean, default=False)
    weather_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    tide_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    ferry_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    traveler_note: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[str] = mapped_column(Text, default="")
    source_url: Mapped[str] = mapped_column(String(1000), default="")
    coordinate_source: Mapped[str] = mapped_column(String(60), default="manual")
    coordinate_crs: Mapped[str] = mapped_column(String(20), default="WGS84")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    region: Mapped[Region] = relationship(back_populates="places")
    creator: Mapped[Optional[User]] = relationship(back_populates="places")
    favorites: Mapped[list["Favorite"]] = relationship(back_populates="place", cascade="all, delete-orphan")
    trip_stops: Mapped[list["TripStop"]] = relationship(back_populates="place", cascade="all, delete-orphan")


class Favorite(Base):
    __tablename__ = "favorites"
    __table_args__ = (UniqueConstraint("user_id", "place_id", name="uq_favorite_user_place"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    place_id: Mapped[int] = mapped_column(ForeignKey("places.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="favorites")
    place: Mapped[Place] = relationship(back_populates="favorites")


class TripStop(Base):
    __tablename__ = "trip_stops"
    __table_args__ = (UniqueConstraint("user_id", "place_id", name="uq_trip_user_place"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    place_id: Mapped[int] = mapped_column(ForeignKey("places.id", ondelete="CASCADE"), index=True)
    day_number: Mapped[int] = mapped_column(Integer, default=1)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="trip_stops")
    place: Mapped[Place] = relationship(back_populates="trip_stops")


class ChatMessage(Base):
    """Per-user island travel conversation history."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    region_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("regions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    role: Mapped[str] = mapped_column(String(20), index=True)
    content: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(100), default="")
    place_ids: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class RegionSnapshot(Base):
    """Latest traveler-facing weather snapshot collected by the batch task."""

    __tablename__ = "region_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id", ondelete="CASCADE"), index=True)
    temperature_c: Mapped[float] = mapped_column(Float)
    precipitation_mm: Mapped[float] = mapped_column(Float, default=0)
    wind_kph: Mapped[float] = mapped_column(Float, default=0)
    weather_code: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(String(120), default="")
    source_url: Mapped[str] = mapped_column(String(1000), default="https://open-meteo.com/")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    region: Mapped[Region] = relationship()


class BatchRun(Base):
    """Auditable execution record for scheduled and manual maintenance runs."""

    __tablename__ = "batch_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), default="travel_conditions", index=True)
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)
    trigger: Mapped[str] = mapped_column(String(20), default="schedule")
    scanned_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
