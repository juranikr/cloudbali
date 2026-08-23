from __future__ import annotations

import secrets
from datetime import date, datetime, time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from app.auth import get_current_user
from app.db import get_db
from app.itinerary_models import TravelPlan, TravelPlanDay, TravelPlanItem, TravelPlanMember
from app.models import Place, User


router = APIRouter(tags=["itineraries"])
PlanVisibility = Literal["private", "shared", "public"]
MemberRole = Literal["editor", "viewer"]


def _clean_title(value: str) -> str:
    value = " ".join(value.split())
    if not value:
        raise ValueError("제목을 입력해 주세요")
    return value


def _validate_date_range(start_date: date, end_date: date) -> None:
    if end_date < start_date:
        raise ValueError("종료일은 시작일보다 빠를 수 없습니다")
    if (end_date - start_date).days > 365:
        raise ValueError("여행 계획은 최대 366일까지 만들 수 있습니다")


def _validate_times(start_time: time | None, end_time: time | None) -> None:
    if end_time is not None and start_time is None:
        raise ValueError("종료 시간을 입력하려면 시작 시간도 필요합니다")
    if start_time is not None and end_time is not None and end_time <= start_time:
        raise ValueError("종료 시간은 시작 시간보다 늦어야 합니다")


class TravelPlanCreate(BaseModel):
    title: str = Field(min_length=1, max_length=180)
    description: str = Field(default="", max_length=5000)
    visibility: PlanVisibility = "private"
    timezone: Literal["Asia/Makassar"] = "Asia/Makassar"
    start_date: date
    end_date: date

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return _clean_title(value)

    @model_validator(mode="after")
    def validate_dates(self):
        _validate_date_range(self.start_date, self.end_date)
        return self


class TravelPlanUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=180)
    description: str | None = Field(default=None, max_length=5000)
    visibility: PlanVisibility | None = None
    timezone: Literal["Asia/Makassar"] | None = None
    start_date: date | None = None
    end_date: date | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        return _clean_title(value) if value is not None else None

    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        for field_name in self.model_fields_set:
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} 값은 null일 수 없습니다")
        return self


class TravelPlanDayCreate(BaseModel):
    calendar_date: date
    title: str = Field(default="", max_length=180)
    note: str = Field(default="", max_length=5000)
    sort_order: int | None = Field(default=None, ge=0, le=100000)


class TravelPlanDayUpdate(BaseModel):
    calendar_date: date | None = None
    title: str | None = Field(default=None, max_length=180)
    note: str | None = Field(default=None, max_length=5000)
    sort_order: int | None = Field(default=None, ge=0, le=100000)

    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        for field_name in self.model_fields_set:
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} 값은 null일 수 없습니다")
        return self


class TravelPlanItemCreate(BaseModel):
    place_id: int = Field(gt=0)
    start_time: time | None = None
    end_time: time | None = None
    sort_order: int | None = Field(default=None, ge=0, le=100000)
    note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_time_range(self):
        _validate_times(self.start_time, self.end_time)
        return self


class TravelPlanItemUpdate(BaseModel):
    day_id: int | None = Field(default=None, gt=0)
    place_id: int | None = Field(default=None, gt=0)
    start_time: time | None = None
    end_time: time | None = None
    sort_order: int | None = Field(default=None, ge=0, le=100000)
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def reject_invalid_nulls(self):
        for field_name in self.model_fields_set - {"start_time", "end_time"}:
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} 값은 null일 수 없습니다")
        return self


class TravelPlanItemsReorder(BaseModel):
    item_ids: list[int] = Field(max_length=500)

    @field_validator("item_ids")
    @classmethod
    def validate_item_ids(cls, value: list[int]) -> list[int]:
        if any(item_id <= 0 for item_id in value):
            raise ValueError("일정 장소 ID는 1 이상이어야 합니다")
        if len(value) != len(set(value)):
            raise ValueError("같은 일정 장소 ID를 중복해서 보낼 수 없습니다")
        return value


class TravelPlanMemberInvite(BaseModel):
    email: EmailStr
    role: MemberRole = "viewer"


class UserBriefOut(BaseModel):
    id: int
    display_name: str
    email: str | None = None


class ItineraryPlaceOut(BaseModel):
    id: int
    region_id: int
    region_name: str
    island: str
    category: str
    title: str
    local_name: str
    lat: float
    lng: float


class TravelPlanItemOut(BaseModel):
    id: int
    day_id: int
    place: ItineraryPlaceOut
    start_time: time | None
    end_time: time | None
    sort_order: int
    note: str
    creator: UserBriefOut | None
    created_at: datetime
    updated_at: datetime


class TravelPlanDayOut(BaseModel):
    id: int
    plan_id: int
    calendar_date: date
    title: str
    note: str
    sort_order: int
    items: list[TravelPlanItemOut]
    created_at: datetime
    updated_at: datetime


class TravelPlanMemberOut(BaseModel):
    id: int
    user: UserBriefOut
    role: MemberRole
    invited_by_email: str | None
    created_at: datetime


class TravelPlanSummaryOut(BaseModel):
    id: int
    owner: UserBriefOut
    title: str
    description: str
    visibility: PlanVisibility
    timezone: str
    start_date: date
    end_date: date
    current_role: str
    can_edit: bool
    member_count: int
    day_count: int
    share_token: str | None
    created_at: datetime
    updated_at: datetime


class TravelPlanDetailOut(TravelPlanSummaryOut):
    members: list[TravelPlanMemberOut]
    days: list[TravelPlanDayOut]


class ShareTokenOut(BaseModel):
    share_token: str
    visibility: PlanVisibility
    public_path: str


def _plan_options():
    return (
        joinedload(TravelPlan.owner),
        selectinload(TravelPlan.members).joinedload(TravelPlanMember.user),
        selectinload(TravelPlan.members).joinedload(TravelPlanMember.invited_by),
        selectinload(TravelPlan.days)
        .selectinload(TravelPlanDay.items)
        .joinedload(TravelPlanItem.place)
        .joinedload(Place.region),
        selectinload(TravelPlan.days)
        .selectinload(TravelPlanDay.items)
        .joinedload(TravelPlanItem.creator),
    )


def _load_plan(db: Session, plan_id: int) -> TravelPlan | None:
    return db.query(TravelPlan).options(*_plan_options()).filter(TravelPlan.id == plan_id).first()


def _role_for(plan: TravelPlan, user: User) -> str | None:
    if plan.owner_id == user.id:
        return "owner"
    return next((member.role for member in plan.members if member.user_id == user.id), None)


def _require_plan(
    db: Session,
    plan_id: int,
    user: User,
    *,
    edit: bool = False,
    owner_only: bool = False,
) -> tuple[TravelPlan, str]:
    plan = _load_plan(db, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="여행 계획을 찾을 수 없습니다")
    role = _role_for(plan, user)
    if owner_only and role != "owner":
        raise HTTPException(status_code=403, detail="여행 계획 소유자만 할 수 있습니다")
    if edit and role not in {"owner", "editor"}:
        raise HTTPException(status_code=403, detail="일정을 편집할 권한이 없습니다")
    if not edit and not owner_only and role is None and plan.visibility != "public":
        raise HTTPException(status_code=403, detail="이 여행 계획을 볼 권한이 없습니다")
    return plan, role or "public"


def _user_brief(user: User, *, include_email: bool = True) -> UserBriefOut:
    return UserBriefOut(
        id=user.id,
        display_name=user.display_name,
        email=user.email if include_email else None,
    )


def _place_out(place: Place) -> ItineraryPlaceOut:
    return ItineraryPlaceOut(
        id=place.id,
        region_id=place.region_id,
        region_name=place.region.name_ko,
        island=place.region.island,
        category=place.category,
        title=place.title,
        local_name=place.local_name,
        lat=place.lat,
        lng=place.lng,
    )


def _item_out(item: TravelPlanItem, *, public: bool = False) -> TravelPlanItemOut:
    return TravelPlanItemOut(
        id=item.id,
        day_id=item.day_id,
        place=_place_out(item.place),
        start_time=item.start_time,
        end_time=item.end_time,
        sort_order=item.sort_order,
        note=item.note,
        creator=_user_brief(item.creator, include_email=not public) if item.creator else None,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _day_out(day: TravelPlanDay, *, public: bool = False) -> TravelPlanDayOut:
    return TravelPlanDayOut(
        id=day.id,
        plan_id=day.plan_id,
        calendar_date=day.calendar_date,
        title=day.title,
        note=day.note,
        sort_order=day.sort_order,
        items=[_item_out(item, public=public) for item in day.items],
        created_at=day.created_at,
        updated_at=day.updated_at,
    )


def _member_out(member: TravelPlanMember) -> TravelPlanMemberOut:
    return TravelPlanMemberOut(
        id=member.id,
        user=_user_brief(member.user),
        role=member.role,
        invited_by_email=member.invited_by.email if member.invited_by else None,
        created_at=member.created_at,
    )


def _plan_out(
    plan: TravelPlan,
    role: str,
    *,
    detail: bool = False,
    public: bool = False,
) -> TravelPlanSummaryOut | TravelPlanDetailOut:
    values = dict(
        id=plan.id,
        owner=_user_brief(plan.owner, include_email=not public),
        title=plan.title,
        description=plan.description,
        visibility=plan.visibility,
        timezone=plan.timezone,
        start_date=plan.start_date,
        end_date=plan.end_date,
        current_role=role,
        can_edit=role in {"owner", "editor"},
        member_count=len(plan.members),
        day_count=len(plan.days),
        share_token=plan.share_token if role == "owner" and not public else None,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
    )
    if detail:
        return TravelPlanDetailOut(
            **values,
            members=[] if public else [_member_out(member) for member in plan.members],
            days=[_day_out(day, public=public) for day in plan.days],
        )
    return TravelPlanSummaryOut(**values)


def _day_for_plan(db: Session, plan_id: int, day_id: int) -> TravelPlanDay:
    day = db.query(TravelPlanDay).filter(
        TravelPlanDay.id == day_id,
        TravelPlanDay.plan_id == plan_id,
    ).first()
    if day is None:
        raise HTTPException(status_code=404, detail="여행 날짜를 찾을 수 없습니다")
    return day


def _item_for_plan(db: Session, plan_id: int, item_id: int) -> TravelPlanItem:
    item = (
        db.query(TravelPlanItem)
        .join(TravelPlanDay, TravelPlanDay.id == TravelPlanItem.day_id)
        .filter(TravelPlanItem.id == item_id, TravelPlanDay.plan_id == plan_id)
        .first()
    )
    if item is None:
        raise HTTPException(status_code=404, detail="일정 장소 항목을 찾을 수 없습니다")
    return item


def _commit_unique(db: Session, detail: str) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=detail) from exc


@router.get("/api/shared-itineraries/{share_token}", response_model=TravelPlanDetailOut)
def shared_itinerary(
    share_token: str,
    db: Session = Depends(get_db),
) -> TravelPlanDetailOut:
    if not 20 <= len(share_token) <= 120:
        raise HTTPException(status_code=404, detail="공유 일정을 찾을 수 없습니다")
    plan = (
        db.query(TravelPlan)
        .options(*_plan_options())
        .filter(
            TravelPlan.share_token == share_token,
            TravelPlan.visibility.in_(("shared", "public")),
        )
        .first()
    )
    if plan is None:
        raise HTTPException(status_code=404, detail="공유 일정을 찾을 수 없습니다")
    return _plan_out(plan, "public", detail=True, public=True)


@router.get("/api/itineraries", response_model=list[TravelPlanSummaryOut])
def my_itineraries(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[TravelPlanSummaryOut]:
    plans = (
        db.query(TravelPlan)
        .options(*_plan_options())
        .filter(
            or_(
                TravelPlan.owner_id == user.id,
                TravelPlan.members.any(TravelPlanMember.user_id == user.id),
            )
        )
        .order_by(TravelPlan.start_date.desc(), TravelPlan.updated_at.desc(), TravelPlan.id.desc())
        .all()
    )
    return [_plan_out(plan, _role_for(plan, user) or "viewer") for plan in plans]


@router.post("/api/itineraries", response_model=TravelPlanDetailOut, status_code=status.HTTP_201_CREATED)
def create_itinerary(
    body: TravelPlanCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TravelPlanDetailOut:
    plan = TravelPlan(owner_id=user.id, **body.model_dump())
    db.add(plan)
    db.commit()
    return _plan_out(_load_plan(db, plan.id), "owner", detail=True)


@router.get("/api/itineraries/{plan_id}", response_model=TravelPlanDetailOut)
def itinerary_detail(
    plan_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TravelPlanDetailOut:
    plan, role = _require_plan(db, plan_id, user)
    return _plan_out(plan, role, detail=True, public=role == "public")


@router.patch("/api/itineraries/{plan_id}", response_model=TravelPlanDetailOut)
def update_itinerary(
    plan_id: int,
    body: TravelPlanUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TravelPlanDetailOut:
    plan, role = _require_plan(db, plan_id, user, edit=True)
    values = body.model_dump(exclude_unset=True)
    next_start = values.get("start_date", plan.start_date)
    next_end = values.get("end_date", plan.end_date)
    try:
        _validate_date_range(next_start, next_end)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    outside_days = [day.calendar_date for day in plan.days if not next_start <= day.calendar_date <= next_end]
    if outside_days:
        raise HTTPException(status_code=409, detail="변경할 날짜 범위 밖에 이미 일정이 있습니다")
    if role != "owner" and "visibility" in values:
        raise HTTPException(status_code=403, detail="공개 범위는 여행 계획 소유자만 바꿀 수 있습니다")
    if values.get("visibility") == "private":
        plan.share_token = None
    for key, value in values.items():
        setattr(plan, key, value)
    db.commit()
    return _plan_out(_load_plan(db, plan.id), role, detail=True)


@router.delete("/api/itineraries/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_itinerary(
    plan_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    plan, _ = _require_plan(db, plan_id, user, owner_only=True)
    db.delete(plan)
    db.commit()
    return Response(status_code=204)


@router.post(
    "/api/itineraries/{plan_id}/days",
    response_model=TravelPlanDayOut,
    status_code=status.HTTP_201_CREATED,
)
def create_itinerary_day(
    plan_id: int,
    body: TravelPlanDayCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TravelPlanDayOut:
    plan, _ = _require_plan(db, plan_id, user, edit=True)
    if not plan.start_date <= body.calendar_date <= plan.end_date:
        raise HTTPException(status_code=422, detail="여행 날짜는 계획의 시작일과 종료일 사이여야 합니다")
    values = body.model_dump()
    if values["sort_order"] is None:
        values["sort_order"] = (body.calendar_date - plan.start_date).days * 10
    day = TravelPlanDay(plan_id=plan.id, **values)
    db.add(day)
    _commit_unique(db, "해당 날짜의 일정이 이미 있습니다")
    refreshed = _load_plan(db, plan.id)
    return _day_out(next(item for item in refreshed.days if item.id == day.id))


@router.patch("/api/itineraries/{plan_id}/days/{day_id}", response_model=TravelPlanDayOut)
def update_itinerary_day(
    plan_id: int,
    day_id: int,
    body: TravelPlanDayUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TravelPlanDayOut:
    plan, _ = _require_plan(db, plan_id, user, edit=True)
    day = _day_for_plan(db, plan_id, day_id)
    values = body.model_dump(exclude_unset=True)
    next_date = values.get("calendar_date", day.calendar_date)
    if not plan.start_date <= next_date <= plan.end_date:
        raise HTTPException(status_code=422, detail="여행 날짜는 계획의 시작일과 종료일 사이여야 합니다")
    for key, value in values.items():
        setattr(day, key, value)
    _commit_unique(db, "해당 날짜의 일정이 이미 있습니다")
    refreshed = _load_plan(db, plan.id)
    return _day_out(next(item for item in refreshed.days if item.id == day_id))


@router.delete("/api/itineraries/{plan_id}/days/{day_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_itinerary_day(
    plan_id: int,
    day_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    _require_plan(db, plan_id, user, edit=True)
    day = _day_for_plan(db, plan_id, day_id)
    db.delete(day)
    db.commit()
    return Response(status_code=204)


@router.post(
    "/api/itineraries/{plan_id}/days/{day_id}/items",
    response_model=TravelPlanItemOut,
    status_code=status.HTTP_201_CREATED,
)
def create_itinerary_item(
    plan_id: int,
    day_id: int,
    body: TravelPlanItemCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TravelPlanItemOut:
    plan, _ = _require_plan(db, plan_id, user, edit=True)
    day = _day_for_plan(db, plan.id, day_id)
    place = db.get(Place, body.place_id)
    if place is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    values = body.model_dump()
    if values["sort_order"] is None:
        values["sort_order"] = (
            db.query(func.max(TravelPlanItem.sort_order)).filter(TravelPlanItem.day_id == day.id).scalar() or 0
        ) + 10
    item = TravelPlanItem(day_id=day.id, creator_id=user.id, **values)
    db.add(item)
    db.commit()
    refreshed = _load_plan(db, plan.id)
    return _item_out(next(item_row for day_row in refreshed.days for item_row in day_row.items if item_row.id == item.id))


@router.patch("/api/itineraries/{plan_id}/items/{item_id}", response_model=TravelPlanItemOut)
def update_itinerary_item(
    plan_id: int,
    item_id: int,
    body: TravelPlanItemUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TravelPlanItemOut:
    plan, _ = _require_plan(db, plan_id, user, edit=True)
    item = _item_for_plan(db, plan.id, item_id)
    values = body.model_dump(exclude_unset=True)
    if "day_id" in values:
        _day_for_plan(db, plan.id, values["day_id"])
    if "place_id" in values and db.get(Place, values["place_id"]) is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    next_start = values.get("start_time", item.start_time)
    next_end = values.get("end_time", item.end_time)
    try:
        _validate_times(next_start, next_end)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    for key, value in values.items():
        setattr(item, key, value)
    db.commit()
    refreshed = _load_plan(db, plan.id)
    return _item_out(next(item_row for day_row in refreshed.days for item_row in day_row.items if item_row.id == item_id))


@router.put(
    "/api/itineraries/{plan_id}/days/{day_id}/items/reorder",
    response_model=list[TravelPlanItemOut],
)
def reorder_itinerary_items(
    plan_id: int,
    day_id: int,
    body: TravelPlanItemsReorder,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[TravelPlanItemOut]:
    plan, _ = _require_plan(db, plan_id, user, edit=True)
    day = (
        db.query(TravelPlanDay)
        .filter(TravelPlanDay.id == day_id, TravelPlanDay.plan_id == plan.id)
        .with_for_update()
        .first()
    )
    if day is None:
        raise HTTPException(status_code=404, detail="여행 날짜를 찾을 수 없습니다")

    items = (
        db.query(TravelPlanItem)
        .filter(TravelPlanItem.day_id == day.id)
        .order_by(TravelPlanItem.sort_order, TravelPlanItem.id)
        .with_for_update()
        .all()
    )
    current_ids = {item.id for item in items}
    if len(body.item_ids) != len(items) or set(body.item_ids) != current_ids:
        raise HTTPException(
            status_code=409,
            detail="일정 장소 구성이 변경되었습니다. 일정을 새로고침한 뒤 다시 순서를 바꿔 주세요",
        )

    items_by_id = {item.id: item for item in items}
    for index, item_id in enumerate(body.item_ids, start=1):
        items_by_id[item_id].sort_order = index * 10
    db.commit()

    refreshed = _load_plan(db, plan.id)
    refreshed_day = next(day_row for day_row in refreshed.days if day_row.id == day.id)
    return [_item_out(item) for item in refreshed_day.items]


@router.delete("/api/itineraries/{plan_id}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_itinerary_item(
    plan_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    _require_plan(db, plan_id, user, edit=True)
    item = _item_for_plan(db, plan_id, item_id)
    db.delete(item)
    db.commit()
    return Response(status_code=204)


@router.post("/api/itineraries/{plan_id}/members", response_model=TravelPlanMemberOut)
def invite_itinerary_member(
    plan_id: int,
    body: TravelPlanMemberInvite,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TravelPlanMemberOut:
    plan, _ = _require_plan(db, plan_id, user, owner_only=True)
    invited = db.query(User).filter(func.lower(User.email) == body.email.lower()).first()
    if invited is None:
        raise HTTPException(status_code=404, detail="해당 이메일의 사용자를 찾을 수 없습니다")
    if invited.id == plan.owner_id:
        raise HTTPException(status_code=409, detail="소유자는 이미 모든 권한을 가지고 있습니다")
    member = db.query(TravelPlanMember).filter(
        TravelPlanMember.plan_id == plan.id,
        TravelPlanMember.user_id == invited.id,
    ).first()
    if member is None:
        member = TravelPlanMember(
            plan_id=plan.id,
            user_id=invited.id,
            role=body.role,
            invited_by_id=user.id,
        )
        db.add(member)
    else:
        member.role = body.role
        member.invited_by_id = user.id
    db.commit()
    refreshed = _load_plan(db, plan.id)
    return _member_out(next(row for row in refreshed.members if row.user_id == invited.id))


@router.delete("/api/itineraries/{plan_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_itinerary_member(
    plan_id: int,
    member_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    plan, _ = _require_plan(db, plan_id, user, owner_only=True)
    member = db.query(TravelPlanMember).filter(
        TravelPlanMember.id == member_id,
        TravelPlanMember.plan_id == plan.id,
    ).first()
    if member is None:
        raise HTTPException(status_code=404, detail="공유 멤버를 찾을 수 없습니다")
    db.delete(member)
    db.commit()
    return Response(status_code=204)


@router.post("/api/itineraries/{plan_id}/share", response_model=ShareTokenOut)
def create_itinerary_share_token(
    plan_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ShareTokenOut:
    plan, _ = _require_plan(db, plan_id, user, owner_only=True)
    token = secrets.token_urlsafe(32)
    while db.query(TravelPlan.id).filter(TravelPlan.share_token == token).first():
        token = secrets.token_urlsafe(32)
    plan.share_token = token
    if plan.visibility == "private":
        plan.visibility = "shared"
    db.commit()
    return ShareTokenOut(
        share_token=token,
        visibility=plan.visibility,
        public_path="/api/shared-itineraries/" + token,
    )


@router.delete("/api/itineraries/{plan_id}/share", status_code=status.HTTP_204_NO_CONTENT)
def revoke_itinerary_share_token(
    plan_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    plan, _ = _require_plan(db, plan_id, user, owner_only=True)
    plan.share_token = None
    if plan.visibility == "shared":
        plan.visibility = "private"
    db.commit()
    return Response(status_code=204)
