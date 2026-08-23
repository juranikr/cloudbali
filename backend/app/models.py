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


class DiscoveryJob(Base):
    """Durable parameters and counters for an asynchronous discovery run."""

    __tablename__ = "discovery_jobs"
    __table_args__ = (
        UniqueConstraint("active_slot", name="uq_discovery_job_active_slot"),
    )

    batch_run_id: Mapped[int] = mapped_column(
        ForeignKey("batch_runs.id", ondelete="CASCADE"), primary_key=True
    )
    region_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("regions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Only the currently queued/running job owns this global value. SQL's
    # nullable unique semantics retain every terminal job while preventing two
    # workers (HTTP, CLI, or scheduler) from starting discovery concurrently.
    active_slot: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    requested_limit: Mapped[int] = mapped_column(Integer)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    invalid_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    batch_run: Mapped[BatchRun] = relationship()
    region: Mapped[Optional[Region]] = relationship()


class DiscoveryScanState(Base):
    """Persistent expanding OSM window for one discovery source and region."""

    __tablename__ = "discovery_scan_states"
    __table_args__ = (
        UniqueConstraint("source", "region_id", name="uq_discovery_scan_source_region"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(40), default="openstreetmap", index=True)
    region_id: Mapped[int] = mapped_column(
        ForeignKey("regions.id", ondelete="CASCADE"), index=True
    )
    query_phase: Mapped[int] = mapped_column(Integer, default=0)
    fetch_limit: Mapped[int] = mapped_column(Integer, default=0)
    scan_count: Mapped[int] = mapped_column(Integer, default=0)
    last_result_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    region: Mapped[Region] = relationship()


class DiscoveryCandidate(Base):
    """A sourced place candidate that is never public until an admin approves it."""

    __tablename__ = "discovery_candidates"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_discovery_candidate_source_external"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    discovery_run_id: Mapped[int] = mapped_column(
        ForeignKey("batch_runs.id", ondelete="CASCADE"), index=True
    )
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id", ondelete="RESTRICT"), index=True)
    source: Mapped[str] = mapped_column(String(40), default="openstreetmap", index=True)
    external_id: Mapped[str] = mapped_column(String(120))
    source_url: Mapped[str] = mapped_column(String(1000), default="")
    title: Mapped[str] = mapped_column(String(180))
    local_name: Mapped[str] = mapped_column(String(180), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    area: Mapped[str] = mapped_column(String(100), default="")
    category: Mapped[str] = mapped_column(String(30), index=True)
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    evidence: Mapped[str] = mapped_column(Text, default="{}")
    tags: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    duplicate_place_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"), nullable=True, index=True
    )
    result_place_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"), nullable=True, index=True
    )
    decision_note: Mapped[str] = mapped_column(Text, default="")
    decided_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    discovery_run: Mapped[BatchRun] = relationship()
    region: Mapped[Region] = relationship()
    duplicate_place: Mapped[Optional[Place]] = relationship(foreign_keys=[duplicate_place_id])
    result_place: Mapped[Optional[Place]] = relationship(foreign_keys=[result_place_id])
    decided_by: Mapped[Optional[User]] = relationship(foreign_keys=[decided_by_id])
    decisions: Mapped[list["DiscoveryDecision"]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan", order_by="DiscoveryDecision.id"
    )


class DiscoveryDecision(Base):
    """Append-only approval trail for a discovery candidate."""

    __tablename__ = "discovery_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("discovery_candidates.id", ondelete="CASCADE"), index=True
    )
    admin_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(30), index=True)
    from_status: Mapped[str] = mapped_column(String(20), default="")
    to_status: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(Text, default="")
    place_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    candidate: Mapped[DiscoveryCandidate] = relationship(back_populates="decisions")
    admin: Mapped[Optional[User]] = relationship(foreign_keys=[admin_id])
    place: Mapped[Optional[Place]] = relationship(foreign_keys=[place_id])
