from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field, ValidationError, field_validator, model_validator
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_admin_user, get_current_user, hash_password
from app.config import settings
from app.db import get_db
from app.extended_models import PlaceAppeal, PlaceChangeEvent, PlaceImage, PlaceNote
from app.itinerary_models import TravelPlan
from app.models import Favorite, Place, Region, TripStop, User


router = APIRouter(tags=["operations"])


class AdminUserCreate(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=72)


class AdminUserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    password: str | None = Field(default=None, min_length=8, max_length=72)

    @model_validator(mode="after")
    def require_change(self) -> "AdminUserUpdate":
        if self.display_name is None and self.password is None:
            raise ValueError("변경할 이름 또는 비밀번호가 필요합니다")
        return self


class AdminUserOut(BaseModel):
    id: int
    email: str
    display_name: str
    place_count: int
    favorite_count: int
    trip_stop_count: int
    owned_plan_count: int
    note_count: int
    image_count: int
    appeal_count: int
    is_admin: bool
    created_at: datetime


class NoteCreate(BaseModel):
    content: str = Field(min_length=1, max_length=5000)

    @field_validator("content")
    @classmethod
    def clean_content(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("메모 내용을 입력해 주세요")
        return cleaned


class NoteUpdate(NoteCreate):
    pass


class NoteOut(BaseModel):
    id: int
    place_id: int
    user_id: int
    author_name: str
    content: str
    is_mine: bool
    can_edit: bool
    created_at: datetime
    updated_at: datetime


def _validate_https_url(value: str, *, allow_blank: bool) -> str:
    cleaned = value.strip()
    if allow_blank and not cleaned:
        return ""
    try:
        parsed = urlsplit(cleaned)
        valid = parsed.scheme.lower() == "https" and bool(parsed.hostname) and not parsed.username
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("HTTPS URL만 사용할 수 있습니다")
    return cleaned


class ImageCreate(BaseModel):
    image_url: str = Field(min_length=1, max_length=2000)
    caption: str = Field(default="", max_length=300)
    source_url: str = Field(default="", max_length=2000)
    sort_order: int | None = Field(default=None, ge=0, le=1_000_000)

    @field_validator("image_url")
    @classmethod
    def image_must_be_https(cls, value: str) -> str:
        return _validate_https_url(value, allow_blank=False)

    @field_validator("source_url")
    @classmethod
    def source_must_be_https(cls, value: str) -> str:
        return _validate_https_url(value, allow_blank=True)

    @field_validator("caption")
    @classmethod
    def clean_caption(cls, value: str) -> str:
        return value.strip()


class ImageUpdate(BaseModel):
    image_url: str | None = Field(default=None, min_length=1, max_length=2000)
    caption: str | None = Field(default=None, max_length=300)
    source_url: str | None = Field(default=None, max_length=2000)
    sort_order: int | None = Field(default=None, ge=0, le=1_000_000)

    @field_validator("image_url")
    @classmethod
    def image_must_be_https(cls, value: str | None) -> str | None:
        return None if value is None else _validate_https_url(value, allow_blank=False)

    @field_validator("source_url")
    @classmethod
    def source_must_be_https(cls, value: str | None) -> str | None:
        return None if value is None else _validate_https_url(value, allow_blank=True)

    @field_validator("caption")
    @classmethod
    def clean_caption(cls, value: str | None) -> str | None:
        return None if value is None else value.strip()

    @model_validator(mode="after")
    def require_change(self) -> "ImageUpdate":
        if not self.model_fields_set:
            raise ValueError("변경할 이미지 정보가 필요합니다")
        if any(getattr(self, field_name) is None for field_name in self.model_fields_set):
            raise ValueError("이미지 수정 값은 null일 수 없습니다")
        return self


class ImageReorder(BaseModel):
    image_ids: list[int] = Field(min_length=1, max_length=200)

    @field_validator("image_ids")
    @classmethod
    def unique_ids(cls, value: list[int]) -> list[int]:
        if any(item <= 0 for item in value) or len(value) != len(set(value)):
            raise ValueError("중복되지 않은 올바른 이미지 ID가 필요합니다")
        return value


class ImageOut(BaseModel):
    id: int
    place_id: int
    user_id: int
    uploader_name: str
    image_url: str
    caption: str
    source_url: str
    sort_order: int
    is_mine: bool
    can_edit: bool
    created_at: datetime
    updated_at: datetime


class ChangeEventOut(BaseModel):
    id: int
    place_id: int | None
    actor_id: int | None
    rollback_of_event_id: int | None
    actor_name: str
    event_type: str
    field_name: str
    old_value: str
    new_value: str
    summary: str
    metadata: dict
    created_at: datetime


class PlaceRollbackSnapshot(BaseModel):
    """The explicit allow-list of Place fields that an audit event may restore."""

    model_config = ConfigDict(extra="forbid")

    region_id: int | None = Field(default=None, gt=0)
    category: str | None = Field(default=None, min_length=1, max_length=30)
    title: str | None = Field(default=None, min_length=1, max_length=180)
    local_name: str | None = Field(default=None, max_length=180)
    description: str | None = Field(default=None, max_length=5000)
    area: str | None = Field(default=None, max_length=100)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    duration_minutes: int | None = Field(default=None, ge=15, le=1440)
    budget_level: int | None = Field(default=None, ge=0, le=4)
    best_time: str | None = Field(default=None, max_length=120)
    access_type: str | None = Field(default=None, max_length=60)
    booking_required: bool | None = None
    weather_sensitive: bool | None = None
    tide_sensitive: bool | None = None
    ferry_sensitive: bool | None = None
    traveler_note: str | None = Field(default=None, max_length=5000)
    tags: str | list[str] | None = None
    source_url: str | None = Field(default=None, max_length=1000)
    coordinate_source: str | None = Field(default=None, max_length=60)
    coordinate_crs: Literal["WGS84"] | None = None

    @model_validator(mode="after")
    def require_concrete_values(self) -> "PlaceRollbackSnapshot":
        if not self.model_fields_set:
            raise ValueError("복원할 이전 값이 없습니다")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("복원 값에는 null을 사용할 수 없습니다")
        if isinstance(self.tags, list):
            cleaned = [str(item).strip() for item in self.tags if str(item).strip()]
            if any(len(item) > 100 for item in cleaned):
                raise ValueError("태그가 너무 깁니다")
            self.tags = cleaned
        return self


class AppealCreate(BaseModel):
    event_id: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=80)
    detail: str = Field(min_length=1, max_length=5000)

    @field_validator("reason", "detail")
    @classmethod
    def clean_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("이의 내용을 입력해 주세요")
        return cleaned


class EventAppealCreate(BaseModel):
    reason: str = Field(min_length=1, max_length=80)
    detail: str = Field(min_length=1, max_length=5000)

    @field_validator("reason", "detail")
    @classmethod
    def clean_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("이의 내용을 입력해 주세요")
        return cleaned


class AppealResolve(BaseModel):
    status: Literal["resolved", "dismissed"]
    resolution: str = Field(min_length=1, max_length=5000)

    @field_validator("resolution")
    @classmethod
    def clean_resolution(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("처리 결과를 입력해 주세요")
        return cleaned


class AppealOut(BaseModel):
    id: int
    event_id: int
    place_id: int | None
    place_title: str
    user_id: int
    user_name: str
    reason: str
    detail: str
    status: str
    resolution: str
    resolved_by_id: int | None
    resolved_by_name: str
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


def _is_admin(user: User) -> bool:
    return user.email.lower() in settings.admin_email_list


def _require_place(db: Session, place_id: int) -> Place:
    place = db.get(Place, place_id)
    if place is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    return place


def _require_owner_or_admin(owner_id: int, user: User) -> None:
    if owner_id != user.id and not _is_admin(user):
        raise HTTPException(status_code=403, detail="본인이 등록한 항목만 관리할 수 있습니다")


def admin_user_out(db: Session, row: User) -> AdminUserOut:
    return AdminUserOut(
        id=row.id,
        email=row.email,
        display_name=row.display_name,
        place_count=db.query(func.count(Place.id)).filter(Place.creator_id == row.id).scalar() or 0,
        favorite_count=db.query(func.count(Favorite.id)).filter(Favorite.user_id == row.id).scalar() or 0,
        trip_stop_count=db.query(func.count(TripStop.id)).filter(TripStop.user_id == row.id).scalar() or 0,
        owned_plan_count=db.query(func.count(TravelPlan.id)).filter(TravelPlan.owner_id == row.id).scalar() or 0,
        note_count=db.query(func.count(PlaceNote.id)).filter(PlaceNote.user_id == row.id).scalar() or 0,
        image_count=db.query(func.count(PlaceImage.id)).filter(PlaceImage.user_id == row.id).scalar() or 0,
        appeal_count=db.query(func.count(PlaceAppeal.id)).filter(PlaceAppeal.user_id == row.id).scalar() or 0,
        is_admin=_is_admin(row),
        created_at=row.created_at,
    )


def _note_out(db: Session, row: PlaceNote, user: User) -> NoteOut:
    author = db.get(User, row.user_id)
    return NoteOut(
        id=row.id,
        place_id=row.place_id,
        user_id=row.user_id,
        author_name=author.display_name if author else "탈퇴한 사용자",
        content=row.content,
        is_mine=row.user_id == user.id,
        can_edit=row.user_id == user.id or _is_admin(user),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _image_out(db: Session, row: PlaceImage, user: User) -> ImageOut:
    uploader = db.get(User, row.user_id)
    return ImageOut(
        id=row.id,
        place_id=row.place_id,
        user_id=row.user_id,
        uploader_name=uploader.display_name if uploader else "탈퇴한 사용자",
        image_url=row.image_url,
        caption=row.caption,
        source_url=row.source_url,
        sort_order=row.sort_order,
        is_mine=row.user_id == user.id,
        can_edit=row.user_id == user.id or _is_admin(user),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _event_out(db: Session, row: PlaceChangeEvent) -> ChangeEventOut:
    actor = db.get(User, row.actor_id) if row.actor_id else None
    try:
        metadata = json.loads(row.metadata_json or "{}")
    except (json.JSONDecodeError, TypeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return ChangeEventOut(
        id=row.id,
        place_id=row.place_id,
        actor_id=row.actor_id,
        rollback_of_event_id=row.rollback_of_event_id,
        actor_name=actor.display_name if actor else "시스템",
        event_type=row.event_type,
        field_name=row.field_name,
        old_value=row.old_value,
        new_value=row.new_value,
        summary=row.summary,
        metadata=metadata,
        created_at=row.created_at,
    )


def _appeal_out(db: Session, row: PlaceAppeal) -> AppealOut:
    place = db.get(Place, row.place_id) if row.place_id is not None else None
    owner = db.get(User, row.user_id)
    resolver = db.get(User, row.resolved_by_id) if row.resolved_by_id else None
    return AppealOut(
        id=row.id,
        event_id=row.event_id,
        place_id=row.place_id,
        place_title=place.title if place else "삭제된 장소",
        user_id=row.user_id,
        user_name=owner.display_name if owner else "탈퇴한 사용자",
        reason=row.reason,
        detail=row.detail,
        status=row.status,
        resolution=row.resolution,
        resolved_by_id=row.resolved_by_id,
        resolved_by_name=resolver.display_name if resolver else "",
        resolved_at=row.resolved_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def record_place_change_event(
    db: Session,
    *,
    place_id: int,
    actor_id: int | None,
    event_type: str,
    summary: str,
    field_name: str = "",
    old_value: str = "",
    new_value: str = "",
    metadata: dict | None = None,
    rollback_of_event_id: int | None = None,
) -> PlaceChangeEvent:
    """Register an audit event in the caller's transaction without committing it.

    Rollback-capable updates use metadata={"before": {...}, "after": {...}}.
    Only fields accepted by PlaceRollbackSnapshot are restored by the admin endpoint.
    """

    row = PlaceChangeEvent(
        place_id=place_id,
        actor_id=actor_id,
        rollback_of_event_id=rollback_of_event_id,
        event_type=event_type[:40],
        field_name=field_name[:80],
        old_value=old_value,
        new_value=new_value,
        summary=summary[:500],
        metadata_json=json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":")),
    )
    db.add(row)
    db.flush()
    return row


@router.get("/api/admin/users", response_model=list[AdminUserOut])
def admin_users(
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
) -> list[AdminUserOut]:
    rows = db.query(User).order_by(User.created_at, User.id).all()
    return [admin_user_out(db, row) for row in rows]


@router.post("/api/admin/users", response_model=AdminUserOut, status_code=status.HTTP_201_CREATED)
def admin_create_user(
    body: AdminUserCreate,
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
) -> AdminUserOut:
    email = str(body.email).strip().lower()
    if db.query(User).filter(func.lower(User.email) == email).first() is not None:
        raise HTTPException(status_code=409, detail="이미 등록된 이메일입니다")
    row = User(
        email=email,
        display_name=body.display_name.strip(),
        password_hash=hash_password(body.password),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="이미 등록된 이메일입니다") from exc
    db.refresh(row)
    return admin_user_out(db, row)


@router.patch("/api/admin/users/{user_id}", response_model=AdminUserOut)
def admin_update_user(
    user_id: int,
    body: AdminUserUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
) -> AdminUserOut:
    row = db.get(User, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="계정을 찾을 수 없습니다")
    if body.display_name is not None:
        row.display_name = body.display_name.strip()
    if body.password is not None:
        row.password_hash = hash_password(body.password)
    db.commit()
    db.refresh(row)
    return admin_user_out(db, row)


@router.delete("/api/admin/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> Response:
    row = db.query(User).filter(User.id == user_id).with_for_update().first()
    if row is None:
        raise HTTPException(status_code=404, detail="계정을 찾을 수 없습니다")
    if row.id == admin.id:
        raise HTTPException(status_code=409, detail="현재 로그인 중인 관리자 계정은 삭제할 수 없습니다")
    if _is_admin(row):
        admin_emails = settings.admin_email_list
        actual_admins = (
            db.query(User)
            .filter(func.lower(User.email).in_(admin_emails))
            .order_by(User.id)
            .with_for_update()
            .all()
        )
        if len(actual_admins) <= 1:
            raise HTTPException(status_code=409, detail="마지막 관리자 계정은 삭제할 수 없습니다")
    protected_counts = {
        "소유 일정": db.query(func.count(TravelPlan.id)).filter(TravelPlan.owner_id == row.id).scalar() or 0,
        "장소 메모": db.query(func.count(PlaceNote.id)).filter(PlaceNote.user_id == row.id).scalar() or 0,
        "장소 이미지": db.query(func.count(PlaceImage.id)).filter(PlaceImage.user_id == row.id).scalar() or 0,
        "이의신청": db.query(func.count(PlaceAppeal.id)).filter(PlaceAppeal.user_id == row.id).scalar() or 0,
    }
    dependencies = [f"{label} {count}건" for label, count in protected_counts.items() if count]
    if dependencies:
        raise HTTPException(
            status_code=409,
            detail="사용자 콘텐츠 보존을 위해 삭제할 수 없습니다: " + ", ".join(dependencies),
        )
    db.delete(row)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/api/places/{place_id}/notes", response_model=list[NoteOut])
def list_place_notes(
    place_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[NoteOut]:
    _require_place(db, place_id)
    rows = db.query(PlaceNote).filter(PlaceNote.place_id == place_id).order_by(PlaceNote.created_at, PlaceNote.id).all()
    return [_note_out(db, row, user) for row in rows]


@router.post("/api/places/{place_id}/notes", response_model=NoteOut, status_code=status.HTTP_201_CREATED)
def create_place_note(
    place_id: int,
    body: NoteCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> NoteOut:
    place = _require_place(db, place_id)
    row = PlaceNote(place_id=place.id, user_id=user.id, content=body.content)
    db.add(row)
    db.flush()
    record_place_change_event(
        db,
        place_id=place.id,
        actor_id=user.id,
        event_type="note_added",
        summary="장소 메모 추가",
        metadata={"note_id": row.id},
    )
    db.commit()
    db.refresh(row)
    return _note_out(db, row, user)


@router.patch("/api/notes/{note_id}", response_model=NoteOut)
@router.patch("/api/place-notes/{note_id}", response_model=NoteOut, include_in_schema=False)
def update_place_note(
    note_id: int,
    body: NoteUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> NoteOut:
    row = db.get(PlaceNote, note_id)
    if row is None:
        raise HTTPException(status_code=404, detail="메모를 찾을 수 없습니다")
    _require_owner_or_admin(row.user_id, user)
    row.content = body.content
    record_place_change_event(
        db,
        place_id=row.place_id,
        actor_id=user.id,
        event_type="note_updated",
        summary="장소 메모 수정",
        metadata={"note_id": row.id},
    )
    db.commit()
    db.refresh(row)
    return _note_out(db, row, user)


@router.delete("/api/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
@router.delete("/api/place-notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT, include_in_schema=False)
def delete_place_note(
    note_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    row = db.get(PlaceNote, note_id)
    if row is None:
        raise HTTPException(status_code=404, detail="메모를 찾을 수 없습니다")
    _require_owner_or_admin(row.user_id, user)
    record_place_change_event(
        db,
        place_id=row.place_id,
        actor_id=user.id,
        event_type="note_deleted",
        summary="장소 메모 삭제",
        metadata={"note_id": row.id},
    )
    db.delete(row)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/api/places/{place_id}/images", response_model=list[ImageOut])
def list_place_images(
    place_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ImageOut]:
    _require_place(db, place_id)
    rows = db.query(PlaceImage).filter(PlaceImage.place_id == place_id).order_by(PlaceImage.sort_order, PlaceImage.id).all()
    return [_image_out(db, row, user) for row in rows]


@router.post("/api/places/{place_id}/images", response_model=ImageOut, status_code=status.HTTP_201_CREATED)
def create_place_image(
    place_id: int,
    body: ImageCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ImageOut:
    place = _require_place(db, place_id)
    max_order = db.query(func.max(PlaceImage.sort_order)).filter(PlaceImage.place_id == place.id).scalar()
    row = PlaceImage(
        place_id=place.id,
        user_id=user.id,
        image_url=body.image_url,
        caption=body.caption,
        source_url=body.source_url,
        sort_order=body.sort_order if body.sort_order is not None else int(max_order or 0) + 10,
    )
    db.add(row)
    db.flush()
    record_place_change_event(
        db,
        place_id=place.id,
        actor_id=user.id,
        event_type="image_added",
        field_name="images",
        new_value=str(row.id),
        summary="장소 이미지 추가",
        metadata={"image_id": row.id, "source_url": row.source_url},
    )
    db.commit()
    db.refresh(row)
    return _image_out(db, row, user)


@router.patch("/api/place-images/{image_id}", response_model=ImageOut)
def update_place_image(
    image_id: int,
    body: ImageUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ImageOut:
    row = db.get(PlaceImage, image_id)
    if row is None:
        raise HTTPException(status_code=404, detail="이미지를 찾을 수 없습니다")
    _require_owner_or_admin(row.user_id, user)
    changes = body.model_dump(exclude_unset=True)
    old_values = {key: getattr(row, key) for key in changes}
    for key, value in changes.items():
        setattr(row, key, value)
    record_place_change_event(
        db,
        place_id=row.place_id,
        actor_id=user.id,
        event_type="image_updated",
        field_name=",".join(changes),
        old_value=json.dumps(old_values, ensure_ascii=False),
        new_value=json.dumps(changes, ensure_ascii=False),
        summary="장소 이미지 정보 수정",
        metadata={"image_id": row.id},
    )
    db.commit()
    db.refresh(row)
    return _image_out(db, row, user)


@router.put("/api/places/{place_id}/images/order", response_model=list[ImageOut])
@router.post("/api/places/{place_id}/images/reorder", response_model=list[ImageOut], include_in_schema=False)
def reorder_place_images(
    place_id: int,
    body: ImageReorder,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ImageOut]:
    _require_place(db, place_id)
    rows = db.query(PlaceImage).filter(PlaceImage.place_id == place_id).order_by(PlaceImage.sort_order, PlaceImage.id).all()
    by_id = {row.id: row for row in rows}
    if set(body.image_ids) != set(by_id):
        raise HTTPException(status_code=422, detail="현재 장소의 전체 이미지 ID를 순서대로 보내야 합니다")
    if not _is_admin(user) and any(row.user_id != user.id for row in rows):
        raise HTTPException(status_code=403, detail="다른 사용자가 등록한 이미지가 있어 전체 순서를 바꿀 수 없습니다")
    before_ids = [row.id for row in rows]
    for index, image_id in enumerate(body.image_ids):
        by_id[image_id].sort_order = (index + 1) * 10
    record_place_change_event(
        db,
        place_id=place_id,
        actor_id=user.id,
        event_type="images_reordered",
        field_name="image_ids",
        old_value=json.dumps(before_ids),
        new_value=json.dumps(body.image_ids),
        summary="장소 이미지 순서 변경",
    )
    db.commit()
    ordered = [by_id[image_id] for image_id in body.image_ids]
    return [_image_out(db, row, user) for row in ordered]


@router.delete("/api/place-images/{image_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_place_image(
    image_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    row = db.get(PlaceImage, image_id)
    if row is None:
        raise HTTPException(status_code=404, detail="이미지를 찾을 수 없습니다")
    _require_owner_or_admin(row.user_id, user)
    record_place_change_event(
        db,
        place_id=row.place_id,
        actor_id=user.id,
        event_type="image_deleted",
        field_name="images",
        old_value=str(row.id),
        summary="장소 이미지 삭제",
        metadata={"image_id": row.id},
    )
    db.delete(row)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/api/places/{place_id}/events", response_model=list[ChangeEventOut])
@router.get("/api/places/{place_id}/change-events", response_model=list[ChangeEventOut], include_in_schema=False)
def list_place_change_events(
    place_id: int,
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[ChangeEventOut]:
    _require_place(db, place_id)
    rows = (
        db.query(PlaceChangeEvent)
        .filter(PlaceChangeEvent.place_id == place_id)
        .order_by(PlaceChangeEvent.created_at.desc(), PlaceChangeEvent.id.desc())
        .limit(limit)
        .all()
    )
    return [_event_out(db, row) for row in rows]


@router.post("/api/admin/place-events/{event_id}/rollback", response_model=ChangeEventOut)
def rollback_place_event(
    event_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> ChangeEventOut:
    event = db.get(PlaceChangeEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="변경 이벤트를 찾을 수 없습니다")
    if event.event_type == "rollback":
        raise HTTPException(status_code=409, detail="롤백 이벤트는 다시 롤백할 수 없습니다")
    if db.query(PlaceChangeEvent.id).filter(PlaceChangeEvent.rollback_of_event_id == event.id).first():
        raise HTTPException(status_code=409, detail="이미 롤백된 변경 이벤트입니다")
    place = db.get(Place, event.place_id)
    if place is None:
        raise HTTPException(status_code=409, detail="장소가 삭제되어 필드 롤백을 적용할 수 없습니다")
    try:
        metadata = json.loads(event.metadata_json or "{}")
        before = metadata.get("before") if isinstance(metadata, dict) else None
        snapshot = PlaceRollbackSnapshot.model_validate(before)
    except (json.JSONDecodeError, TypeError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail="검증 가능한 이전 장소 스냅샷이 없습니다") from exc
    values = snapshot.model_dump(exclude_unset=True)
    if "region_id" in values and db.get(Region, values["region_id"]) is None:
        raise HTTPException(status_code=422, detail="복원 대상 권역을 찾을 수 없습니다")
    if isinstance(values.get("tags"), list):
        values["tags"] = ",".join(dict.fromkeys(values["tags"]))
    target_region = db.get(Region, values.get("region_id", place.region_id))
    target_lat = values.get("lat", place.lat)
    target_lng = values.get("lng", place.lng)
    if target_region is None or not (
        target_region.south <= target_lat <= target_region.north
        and target_region.west <= target_lng <= target_region.east
    ):
        raise HTTPException(status_code=422, detail="이전 좌표가 복원 대상 권역의 지도 범위 밖입니다")
    current_values = {field: getattr(place, field) for field in values}
    for field, value in values.items():
        setattr(place, field, value)
    rollback = record_place_change_event(
        db,
        place_id=place.id,
        actor_id=admin.id,
        event_type="rollback",
        field_name=",".join(values),
        old_value=json.dumps(current_values, ensure_ascii=False),
        new_value=json.dumps(values, ensure_ascii=False),
        summary="장소 변경 이벤트 #" + str(event.id) + " 롤백",
        metadata={
            "source_event_id": event.id,
            "before": current_values,
            "after": values,
        },
        rollback_of_event_id=event.id,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="이미 롤백된 변경 이벤트입니다") from exc
    db.refresh(rollback)
    return _event_out(db, rollback)


def _create_appeal(db: Session, user: User, *, event_id: int, reason: str, detail: str) -> AppealOut:
    event = db.get(PlaceChangeEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="변경 이벤트를 찾을 수 없습니다")
    if db.query(PlaceAppeal).filter(PlaceAppeal.event_id == event.id, PlaceAppeal.user_id == user.id).first():
        raise HTTPException(status_code=409, detail="이미 이의를 제출한 변경입니다")
    row = PlaceAppeal(
        event_id=event.id,
        place_id=event.place_id,
        user_id=user.id,
        reason=reason,
        detail=detail,
        status="open",
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="이미 이의를 제출한 변경입니다") from exc
    db.refresh(row)
    return _appeal_out(db, row)


@router.post("/api/appeals", response_model=AppealOut, status_code=status.HTTP_201_CREATED)
def create_appeal(
    body: AppealCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AppealOut:
    return _create_appeal(db, user, event_id=body.event_id, reason=body.reason, detail=body.detail)


@router.post(
    "/api/place-change-events/{event_id}/appeals",
    response_model=AppealOut,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
def create_event_appeal(
    event_id: int,
    body: EventAppealCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AppealOut:
    return _create_appeal(db, user, event_id=event_id, reason=body.reason, detail=body.detail)


@router.get("/api/appeals/mine", response_model=list[AppealOut])
@router.get("/api/me/appeals", response_model=list[AppealOut], include_in_schema=False)
def my_appeals(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[AppealOut]:
    rows = (
        db.query(PlaceAppeal)
        .filter(PlaceAppeal.user_id == user.id)
        .order_by(PlaceAppeal.created_at.desc(), PlaceAppeal.id.desc())
        .limit(limit)
        .all()
    )
    return [_appeal_out(db, row) for row in rows]


@router.get("/api/admin/appeals", response_model=list[AppealOut])
def admin_appeals(
    appeal_status: Literal["all", "open", "resolved", "dismissed"] = Query(default="open", alias="status"),
    place_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
) -> list[AppealOut]:
    query = db.query(PlaceAppeal)
    if appeal_status != "all":
        query = query.filter(PlaceAppeal.status == appeal_status)
    if place_id is not None:
        query = query.filter(PlaceAppeal.place_id == place_id)
    rows = query.order_by(PlaceAppeal.created_at, PlaceAppeal.id).limit(limit).all()
    return [_appeal_out(db, row) for row in rows]


@router.patch("/api/admin/appeals/{appeal_id}/resolve", response_model=AppealOut)
def resolve_appeal(
    appeal_id: int,
    body: AppealResolve,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> AppealOut:
    row = (
        db.query(PlaceAppeal)
        .filter(PlaceAppeal.id == appeal_id)
        .with_for_update()
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="이의신청을 찾을 수 없습니다")
    if row.status != "open":
        raise HTTPException(status_code=409, detail="이미 처리된 이의신청입니다")
    row.status = body.status
    row.resolution = body.resolution
    row.resolved_by_id = admin.id
    row.resolved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return _appeal_out(db, row)
