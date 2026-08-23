from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, Time, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models import Place, User


class TravelPlan(Base):
    """A dated Bali itinerary with explicit ownership and sharing rules."""

    __tablename__ = "travel_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(180))
    description: Mapped[str] = mapped_column(Text, default="")
    visibility: Mapped[str] = mapped_column(String(20), default="private", index=True)
    timezone: Mapped[str] = mapped_column(String(60), default="Asia/Makassar")
    start_date: Mapped[date] = mapped_column(Date, index=True)
    end_date: Mapped[date] = mapped_column(Date, index=True)
    share_token: Mapped[Optional[str]] = mapped_column(String(120), nullable=True, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    owner: Mapped[User] = relationship(foreign_keys=[owner_id])
    members: Mapped[list["TravelPlanMember"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="TravelPlanMember.id"
    )
    days: Mapped[list["TravelPlanDay"]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="(TravelPlanDay.sort_order, TravelPlanDay.calendar_date, TravelPlanDay.id)",
    )


class TravelPlanMember(Base):
    __tablename__ = "travel_plan_members"
    __table_args__ = (UniqueConstraint("plan_id", "user_id", name="uq_travel_plan_member"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("travel_plans.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20), default="viewer", index=True)
    invited_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    plan: Mapped[TravelPlan] = relationship(back_populates="members")
    user: Mapped[User] = relationship(foreign_keys=[user_id])
    invited_by: Mapped[Optional[User]] = relationship(foreign_keys=[invited_by_id])


class TravelPlanDay(Base):
    __tablename__ = "travel_plan_days"
    __table_args__ = (UniqueConstraint("plan_id", "calendar_date", name="uq_travel_plan_day_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("travel_plans.id", ondelete="CASCADE"), index=True)
    calendar_date: Mapped[date] = mapped_column(Date, index=True)
    title: Mapped[str] = mapped_column(String(180), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    plan: Mapped[TravelPlan] = relationship(back_populates="days")
    items: Mapped[list["TravelPlanItem"]] = relationship(
        back_populates="day",
        cascade="all, delete-orphan",
        order_by="(TravelPlanItem.sort_order, TravelPlanItem.start_time, TravelPlanItem.id)",
    )


class TravelPlanItem(Base):
    __tablename__ = "travel_plan_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    day_id: Mapped[int] = mapped_column(ForeignKey("travel_plan_days.id", ondelete="CASCADE"), index=True)
    place_id: Mapped[int] = mapped_column(ForeignKey("places.id", ondelete="RESTRICT"), index=True)
    creator_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    start_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    end_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    day: Mapped[TravelPlanDay] = relationship(back_populates="items")
    place: Mapped[Place] = relationship()
    creator: Mapped[Optional[User]] = relationship(foreign_keys=[creator_id])
