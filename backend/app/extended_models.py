from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PlaceNote(Base):
    """A traveler-authored note attached to a place."""

    __tablename__ = "place_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    place_id: Mapped[int] = mapped_column(ForeignKey("places.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(String(20), default="shared", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PlaceImage(Base):
    """A sourced HTTPS image submitted for a place."""

    __tablename__ = "place_images"

    id: Mapped[int] = mapped_column(primary_key=True)
    place_id: Mapped[int] = mapped_column(ForeignKey("places.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    image_url: Mapped[str] = mapped_column(String(2000))
    caption: Mapped[str] = mapped_column(String(300), default="")
    source_url: Mapped[str] = mapped_column(String(2000), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=10, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PlaceChangeEvent(Base):
    """Append-only audit event for traveler-visible place data changes."""

    __tablename__ = "place_change_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    place_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    rollback_of_event_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("place_change_events.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    field_name: Mapped[str] = mapped_column(String(80), default="")
    old_value: Mapped[str] = mapped_column(Text, default="")
    new_value: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(String(500), default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class PlaceAppeal(Base):
    """A user's objection to a specific place change event."""

    __tablename__ = "place_appeals"
    __table_args__ = (UniqueConstraint("event_id", "user_id", name="uq_place_appeal_event_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("place_change_events.id", ondelete="CASCADE"), index=True
    )
    place_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"), nullable=True, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    reason: Mapped[str] = mapped_column(String(80))
    detail: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    resolution: Mapped[str] = mapped_column(Text, default="")
    resolved_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PlaceContributor(Base):
    """Explicit shared-place editor membership."""

    __tablename__ = "place_contributors"
    __table_args__ = (UniqueConstraint("place_id", "user_id", name="uq_place_contributor"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    place_id: Mapped[int] = mapped_column(ForeignKey("places.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20), default="editor")
    added_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlaceChain(Base):
    """A reusable brand/chain identity whose actual branches remain Places."""

    __tablename__ = "place_chains"

    id: Mapped[int] = mapped_column(primary_key=True)
    name_local: Mapped[str] = mapped_column(String(180), index=True)
    name_ko: Mapped[str] = mapped_column(String(180), default="")
    category: Mapped[str] = mapped_column(String(30), default="other", index=True)
    aliases_json: Mapped[str] = mapped_column(Text, default="[]")
    description: Mapped[str] = mapped_column(Text, default="")
    created_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PlaceInsight(Base):
    """A small sourced fact about location, history, visiting or local tips."""

    __tablename__ = "place_insights"

    id: Mapped[int] = mapped_column(primary_key=True)
    place_id: Mapped[int] = mapped_column(ForeignKey("places.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    year_label: Mapped[str] = mapped_column(String(50), default="")
    source_url: Mapped[str] = mapped_column(String(2000))
    source_title: Mapped[str] = mapped_column(String(300), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.7)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserMessage(Base):
    """Private in-app notification with optional place/audit references."""

    __tablename__ = "user_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    place_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"), nullable=True, index=True
    )
    related_event_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("place_change_events.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(40), default="system", index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
