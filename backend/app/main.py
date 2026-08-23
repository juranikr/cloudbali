from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.auth import create_access_token, get_admin_user, get_current_user, verify_password
from app.agent_api import router as agent_router
from app.collaboration import can_edit_place, is_admin
from app.config import settings
from app.db import Base, SessionLocal, engine, get_db
from app.discovery_api import router as discovery_router
from app.extended_models import PlaceContributor, PlaceImage, PlaceInsight, PlaceNote
from app.itinerary_api import router as itinerary_router
from app.itinerary_models import TravelPlanItem
from app.operations_api import record_place_change_event, router as operations_router
from app.parity_api import router as parity_router
from app.migrations import run_migrations
from app.place_identity import distance_m, normalize_place_name, strongest_duplicate
from app.search_service import GeoBounds, search_external_places
from app.batch import run_batch
from app.models import (
    BatchRun,
    ChatMessage,
    ChatWork,
    DiscoveryCandidate,
    Favorite,
    Place,
    Region,
    RegionSnapshot,
    TripStop,
    User,
)
from app.schemas import (
    AdminPlaceUpdate,
    BatchRunOut,
    ChatMessageOut,
    ChatRequest,
    ChatResponse,
    FavoriteOut,
    LoginRequest,
    PlaceCreate,
    PlaceOut,
    PlaceUpdate,
    RegionOut,
    RegionSnapshotOut,
    SearchHit,
    TokenOut,
    TripStopCreate,
    TripStopOut,
    TripStopUpdate,
    UserOut,
)
from app.seed import seed_data
from app.travel_chat import answer_chat, message_dict


@asynccontextmanager
async def lifespan(_: FastAPI):
    run_migrations(engine)
    with SessionLocal() as db:
        seed_data(db)
    yield


app = FastAPI(title=settings.app_name + " API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(discovery_router)
app.include_router(operations_router)
app.include_router(itinerary_router)
app.include_router(agent_router)
# This router must be included before the dynamic ``/api/places/{place_id}``
# route so fixed paths such as ``/api/places/duplicate-candidates`` win.
app.include_router(parity_router)


def tags_out(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def place_out(place: Place, favorite_ids: set[int]) -> PlaceOut:
    return PlaceOut(
        id=place.id,
        region_id=place.region_id,
        region_name=place.region.name_ko,
        island=place.region.island,
        category=place.category,
        title=place.title,
        local_name=place.local_name,
        description=place.description,
        area=place.area,
        lat=place.lat,
        lng=place.lng,
        duration_minutes=place.duration_minutes,
        budget_level=place.budget_level,
        best_time=place.best_time,
        access_type=place.access_type,
        booking_required=place.booking_required,
        weather_sensitive=place.weather_sensitive,
        tide_sensitive=place.tide_sensitive,
        ferry_sensitive=place.ferry_sensitive,
        traveler_note=place.traveler_note,
        tags=tags_out(place.tags),
        source_url=place.source_url,
        coordinate_source=place.coordinate_source,
        coordinate_external_id=place.coordinate_external_id,
        coordinate_confidence=place.coordinate_confidence,
        coordinate_verified_at=place.coordinate_verified_at,
        coordinate_crs=place.coordinate_crs,
        chain_id=place.chain_id,
        branch_name=place.branch_name,
        merged_into_id=place.merged_into_id,
        is_favorite=place.id in favorite_ids,
        is_seed=place.creator_id is None,
        created_at=place.created_at,
    )


def user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_admin=user.email.lower() in settings.admin_email_list,
    )


def apply_place_update_with_audit(
    db: Session,
    *,
    place: Place,
    actor_id: int,
    values: dict,
    summary: str,
) -> list[str]:
    """Apply changed Place fields and keep a validated rollback snapshot."""

    before = {key: getattr(place, key) for key, value in values.items() if getattr(place, key) != value}
    if not before:
        return []
    after = {key: values[key] for key in before}
    for key, value in after.items():
        setattr(place, key, value)
    changed = list(before)
    record_place_change_event(
        db,
        place_id=place.id,
        actor_id=actor_id,
        event_type="place_updated",
        field_name=",".join(changed),
        old_value=json.dumps(before, ensure_ascii=False),
        new_value=json.dumps(after, ensure_ascii=False),
        summary=summary,
        metadata={"before": before, "after": after},
    )
    return changed


def record_place_deletion(db: Session, *, place: Place, actor_id: int, summary: str) -> None:
    record_place_change_event(
        db,
        place_id=place.id,
        actor_id=actor_id,
        event_type="place_deleted",
        summary=summary,
        metadata={
            "deleted_place": {
                "id": place.id,
                "region_id": place.region_id,
                "category": place.category,
                "title": place.title,
                "local_name": place.local_name,
                "lat": place.lat,
                "lng": place.lng,
            }
        },
    )


def user_favorite_ids(db: Session, user_id: int) -> set[int]:
    return {row[0] for row in db.query(Favorite.place_id).filter(Favorite.user_id == user_id).all()}


def load_place(
    db: Session,
    place_id: int,
    *,
    active: bool = True,
    for_update: bool = False,
) -> Place | None:
    query = db.query(Place).filter(Place.id == place_id)
    if not for_update:
        query = query.options(joinedload(Place.region))
    if active:
        query = query.filter(Place.merged_into_id.is_(None))
    if for_update:
        query = query.populate_existing().with_for_update()
    return query.first()


def place_deletion_dependency(db: Session, place_id: int) -> str:
    """Return the first durable relationship that must be removed explicitly."""

    checks = (
        (Favorite, Favorite.place_id, "즐겨찾기"),
        (TripStop, TripStop.place_id, "간이 여행 일정"),
        (TravelPlanItem, TravelPlanItem.place_id, "여행 일정"),
        (PlaceNote, PlaceNote.place_id, "여행자 메모"),
        (PlaceImage, PlaceImage.place_id, "장소 이미지"),
        (PlaceContributor, PlaceContributor.place_id, "공동 편집자"),
        (PlaceInsight, PlaceInsight.place_id, "장소 인사이트"),
        (DiscoveryCandidate, DiscoveryCandidate.result_place_id, "승인된 신규 장소 후보"),
        (Place, Place.merged_into_id, "병합된 원본 장소"),
    )
    for model, field, label in checks:
        if db.query(model).filter(field == place_id).first() is not None:
            return label
    return ""


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "app": settings.app_name, "coordinate_crs": "WGS84"}


@app.post("/api/auth/login", response_model=TokenOut)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> TokenOut:
    user = db.query(User).filter(User.email == body.email.lower()).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="이메일 또는 비밀번호가 올바르지 않습니다")
    return TokenOut(access_token=create_access_token(user.id), user=user_out(user))


@app.get("/api/auth/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return user_out(user)


@app.get("/api/regions", response_model=list[RegionOut])
def regions(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[RegionOut]:
    return [
        RegionOut.model_validate(row)
        for row in db.query(Region).order_by(Region.sort_order, Region.id).all()
    ]


@app.get("/api/places", response_model=list[PlaceOut])
def places(
    region_id: int | None = Query(default=None, gt=0),
    chain_id: int | None = Query(default=None, gt=0),
    island: str | None = None,
    categories: str = "",
    favorites_only: bool = False,
    condition: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[PlaceOut]:
    query = db.query(Place).options(joinedload(Place.region)).filter(Place.merged_into_id.is_(None))
    if region_id:
        query = query.filter(Place.region_id == region_id)
    if chain_id:
        query = query.filter(Place.chain_id == chain_id)
    if island:
        query = query.join(Region).filter(Region.island == island)
    selected_categories = [item for item in categories.split(",") if item]
    if selected_categories:
        query = query.filter(Place.category.in_(selected_categories))
    if condition == "weather":
        query = query.filter(Place.weather_sensitive.is_(True))
    elif condition == "tide":
        query = query.filter(Place.tide_sensitive.is_(True))
    elif condition == "ferry":
        query = query.filter(Place.ferry_sensitive.is_(True))
    elif condition == "booking":
        query = query.filter(Place.booking_required.is_(True))
    favorite_ids = user_favorite_ids(db, user.id)
    rows = query.order_by(Place.region_id, Place.category, Place.title).all()
    if favorites_only:
        rows = [row for row in rows if row.id in favorite_ids]
    return [place_out(row, favorite_ids) for row in rows]


@app.get("/api/places/{place_id}", response_model=PlaceOut)
def get_place(
    place_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PlaceOut:
    row = load_place(db, place_id)
    if row is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    return place_out(row, user_favorite_ids(db, user.id))


@app.post("/api/places", response_model=PlaceOut, status_code=status.HTTP_201_CREATED)
def create_place(
    body: PlaceCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PlaceOut:
    region = db.query(Region).filter(Region.id == body.region_id).populate_existing().with_for_update().first()
    if region is None:
        raise HTTPException(status_code=404, detail="권역을 찾을 수 없습니다")
    if not (region.south <= body.lat <= region.north and region.west <= body.lng <= region.east):
        raise HTTPException(status_code=422, detail="선택한 권역의 지도 범위 밖입니다")
    duplicate = strongest_duplicate(
        db=db,
        title=body.title,
        local_name=body.local_name,
        lat=body.lat,
        lng=body.lng,
        category=body.category,
        region_id=body.region_id,
    )
    if duplicate is not None and duplicate.confidence >= 0.9:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "이미 등록된 것으로 보이는 장소가 있습니다",
                "duplicate": {
                    "place_id": duplicate.place.id,
                    "title": duplicate.place.title,
                    "distance_m": round(duplicate.distance_m, 1),
                    "confidence": duplicate.confidence,
                    "reason": duplicate.reason,
                },
            },
        )
    values = body.model_dump()
    values["tags"] = ",".join(dict.fromkeys(item.strip() for item in values["tags"] if item.strip()))
    row = Place(**values, creator_id=user.id, coordinate_crs="WGS84")
    db.add(row)
    db.flush()
    record_place_change_event(
        db,
        place_id=row.id,
        actor_id=user.id,
        event_type="place_created",
        summary="사용자가 장소를 등록했습니다",
        metadata={"source": "manual"},
    )
    db.commit()
    return place_out(load_place(db, row.id), user_favorite_ids(db, user.id))


@app.patch("/api/places/{place_id}", response_model=PlaceOut)
def update_place(
    place_id: int,
    body: PlaceUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PlaceOut:
    row = load_place(db, place_id, for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    if not can_edit_place(db, row, user):
        raise HTTPException(status_code=403, detail="장소 소유자·공동 편집자·관리자만 수정할 수 있습니다")
    values = body.model_dump(exclude_unset=True)
    if "tags" in values:
        values["tags"] = ",".join(dict.fromkeys(item.strip() for item in values["tags"] if item.strip()))
    apply_place_update_with_audit(
        db,
        place=row,
        actor_id=user.id,
        values=values,
        summary="사용자가 장소 정보를 수정했습니다",
    )
    db.commit()
    return place_out(load_place(db, row.id), user_favorite_ids(db, user.id))


@app.delete("/api/places/{place_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_place(
    place_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    row = load_place(db, place_id, for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    if row.creator_id != user.id and not is_admin(user):
        raise HTTPException(status_code=403, detail="직접 추가한 장소만 삭제할 수 있습니다")
    dependency = place_deletion_dependency(db, row.id)
    if dependency:
        raise HTTPException(
            status_code=409,
            detail=f"{dependency}에서 사용 중인 장소는 삭제할 수 없습니다. 연결 데이터를 먼저 정리해 주세요",
        )
    record_place_deletion(db, place=row, actor_id=user.id, summary="사용자가 장소를 삭제했습니다")
    db.delete(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="다른 여행 데이터에서 사용 중인 장소는 삭제할 수 없습니다",
        ) from exc
    return Response(status_code=204)


@app.put("/api/places/{place_id}/favorite", response_model=FavoriteOut)
def toggle_favorite(
    place_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FavoriteOut:
    if load_place(db, place_id, for_update=True) is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    row = db.query(Favorite).filter(Favorite.user_id == user.id, Favorite.place_id == place_id).first()
    if row:
        db.delete(row)
        value = False
    else:
        db.add(Favorite(user_id=user.id, place_id=place_id))
        value = True
    db.commit()
    return FavoriteOut(place_id=place_id, is_favorite=value)


@app.get("/api/favorites", response_model=list[PlaceOut])
def list_favorites(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[PlaceOut]:
    """Return the user's saved places in most-recently-saved order."""

    rows = (
        db.query(Favorite)
        .join(Place, Place.id == Favorite.place_id)
        .options(joinedload(Favorite.place).joinedload(Place.region))
        .filter(
            Favorite.user_id == user.id,
            Place.merged_into_id.is_(None),
        )
        .order_by(Favorite.created_at.desc(), Favorite.id.desc())
        .all()
    )
    favorite_ids = {row.place_id for row in rows}
    return [place_out(row.place, favorite_ids) for row in rows]


@app.post("/api/favorites/{place_id}", response_model=FavoriteOut)
def add_favorite(
    place_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FavoriteOut:
    """Idempotently save a place without relying on toggle state."""

    if load_place(db, place_id, for_update=True) is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    row = db.query(Favorite.id).filter(
        Favorite.user_id == user.id,
        Favorite.place_id == place_id,
    ).first()
    if row is None:
        db.add(Favorite(user_id=user.id, place_id=place_id))
        try:
            db.commit()
        except IntegrityError:
            # A repeated request racing another tab has the same successful
            # end state, so keep this endpoint idempotent.
            db.rollback()
    return FavoriteOut(place_id=place_id, is_favorite=True)


@app.delete("/api/favorites/{place_id}", response_model=FavoriteOut)
def remove_favorite(
    place_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FavoriteOut:
    """Idempotently remove a saved place."""

    row = db.query(Favorite).filter(
        Favorite.user_id == user.id,
        Favorite.place_id == place_id,
    ).first()
    if row is not None:
        db.delete(row)
        db.commit()
    return FavoriteOut(place_id=place_id, is_favorite=False)


@app.get("/api/search", response_model=list[SearchHit])
async def search(
    q: str = Query(min_length=2, max_length=100),
    region_id: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[SearchHit]:
    local_query = db.query(Place).options(joinedload(Place.region)).filter(
        Place.merged_into_id.is_(None),
        or_(
            Place.title.ilike("%" + q + "%"),
            Place.local_name.ilike("%" + q + "%"),
            Place.area.ilike("%" + q + "%"),
            Place.tags.ilike("%" + q + "%"),
        )
    )
    region = db.get(Region, region_id) if region_id else None
    if region_id is not None and region is None:
        raise HTTPException(status_code=404, detail="권역을 찾을 수 없습니다")
    if region:
        local_query = local_query.filter(Place.region_id == region.id)
    local_rows = local_query.limit(8).all()
    hits = [
        SearchHit(
            key="local-" + str(row.id),
            source="local",
            title=row.title,
            display_name=(row.local_name + " · " + row.region.name_ko).strip(" ·"),
            lat=row.lat,
            lng=row.lng,
            region_id=row.region_id,
            place_id=row.id,
            category=row.category,
            source_url=row.source_url,
            source_urls=[row.source_url] if row.source_url else [],
            external_id=row.coordinate_external_id or f"place:{row.id}",
            external_ids={
                row.coordinate_source or "local": row.coordinate_external_id
            } if row.coordinate_external_id else {"local": f"place:{row.id}"},
            coordinate_source=row.coordinate_source or "local",
            confidence=row.coordinate_confidence if row.coordinate_confidence is not None else 0.78,
            storage_allowed=True,
            attribution=(
                "© OpenStreetMap contributors"
                if "openstreetmap" in (row.coordinate_source or "").lower() else ""
            ),
            license="ODbL 1.0" if "openstreetmap" in (row.coordinate_source or "").lower() else "",
            license_url=(
                "https://www.openstreetmap.org/copyright"
                if "openstreetmap" in (row.coordinate_source or "").lower() else ""
            ),
            sources=["local", row.coordinate_source] if row.coordinate_source else ["local"],
        )
        for row in local_rows
    ]
    bounds = GeoBounds(
        west=region.west,
        south=region.south,
        east=region.east,
        north=region.north,
        name=region.slug,
    ) if region else None
    try:
        remote = await search_external_places(
            q,
            bounds=bounds,
            user_agent=settings.geocoder_user_agent,
            timeout_seconds=settings.geocoder_timeout_seconds,
            per_source_limit=5,
            result_limit=12 - len(hits),
        )
    except Exception:
        remote = []
    for remote_hit in remote:
        matched_index: int | None = None
        remote_name = normalize_place_name(remote_hit.title)
        for index, local_row in enumerate(local_rows):
            meters = distance_m(local_row.lat, local_row.lng, remote_hit.lat, remote_hit.lng)
            local_names = {
                normalize_place_name(local_row.title),
                normalize_place_name(local_row.local_name),
            } - {""}
            same_external_id = bool(
                local_row.coordinate_external_id
                and local_row.coordinate_external_id in remote_hit.external_ids.values()
            )
            if same_external_id or (remote_name in local_names and meters <= 180):
                matched_index = index
                break
        if matched_index is not None:
            local_hit = hits[matched_index]
            local_hit.cross_checked = True
            local_hit.confidence = round(max(0.92, local_hit.confidence, remote_hit.confidence), 3)
            local_hit.sources = list(dict.fromkeys([*local_hit.sources, *remote_hit.sources]))
            local_hit.source_urls = list(dict.fromkeys([*local_hit.source_urls, *remote_hit.source_urls]))
            local_hit.external_ids = {**local_hit.external_ids, **dict(remote_hit.external_ids)}
            local_hit.coordinate_source = "+".join(
                value for value in local_hit.sources if value != "local"
            ) or "local"
            if not local_hit.source_url:
                local_hit.source_url = remote_hit.source_url
            if not local_hit.attribution:
                local_hit.attribution = remote_hit.attribution
            if not local_hit.license:
                local_hit.license = remote_hit.license
            continue
        values = remote_hit.as_dict()
        values["region_id"] = region.id if region else None
        if remote_hit.source == "openstreetmap" or "openstreetmap" in remote_hit.sources:
            values["license_url"] = "https://www.openstreetmap.org/copyright"
        elif remote_hit.source == "wikidata":
            values["license_url"] = "https://www.wikidata.org/wiki/Wikidata:Copyright"
        hits.append(SearchHit(**values))
        if len(hits) >= 12:
            break
    return hits


@app.get("/api/chat", response_model=list[ChatMessageOut])
def chat_history(
    region_id: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ChatMessageOut]:
    query = db.query(ChatMessage).filter(ChatMessage.user_id == user.id)
    if region_id:
        query = query.filter(ChatMessage.region_id == region_id)
    rows = query.order_by(ChatMessage.id.desc()).limit(60).all()[::-1]
    return [ChatMessageOut(**message_dict(row)) for row in rows]


@app.post("/api/chat", response_model=ChatResponse)
def post_chat(
    body: ChatRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ChatResponse:
    try:
        row, grounded, work_state = answer_chat(
            db,
            user=user,
            message=body.message,
            region_id=body.region_id,
            selected_place_id=body.selected_place_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    favorite_ids = user_favorite_ids(db, user.id)
    return ChatResponse(
        message=ChatMessageOut(**message_dict(row)),
        grounded_places=[place_out(load_place(db, place.id), favorite_ids) for place in grounded],
        model=row.model,
        work_state=work_state,
    )


@app.delete("/api/chat", status_code=status.HTTP_204_NO_CONTENT)
def clear_chat(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    db.query(ChatMessage).filter(ChatMessage.user_id == user.id).delete(synchronize_session=False)
    db.query(ChatWork).filter(ChatWork.user_id == user.id).delete(synchronize_session=False)
    db.commit()
    return Response(status_code=204)


@app.get("/api/conditions", response_model=list[RegionSnapshotOut])
def region_conditions(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[RegionSnapshotOut]:
    now = datetime.now(timezone.utc)
    latest_ids = (
        select(func.max(RegionSnapshot.id))
        .group_by(RegionSnapshot.region_id)
    )
    rows = (
        db.query(RegionSnapshot)
        .options(joinedload(RegionSnapshot.region))
        .filter(RegionSnapshot.id.in_(latest_ids))
        .order_by(RegionSnapshot.region_id)
        .all()
    )
    output: list[RegionSnapshotOut] = []
    for row in rows:
        observed_at = (
            row.observed_at
            if row.observed_at.tzinfo is not None
            else row.observed_at.replace(tzinfo=timezone.utc)
        )
        output.append(RegionSnapshotOut(
            id=row.id,
            region_id=row.region_id,
            region_name=row.region.name_ko,
            island=row.region.island,
            temperature_c=row.temperature_c,
            precipitation_mm=row.precipitation_mm,
            wind_kph=row.wind_kph,
            weather_code=row.weather_code,
            summary=row.summary,
            source_url=row.source_url,
            observed_at=observed_at,
            is_stale=now - observed_at > timedelta(hours=12),
        ))
    return output


@app.get("/api/admin/summary")
def admin_summary(
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> dict:
    region_rows = (
        db.query(Region.id, Region.name_ko, Region.island, func.count(Place.id))
        .outerjoin(Place, Place.region_id == Region.id)
        .group_by(Region.id, Region.name_ko, Region.island, Region.sort_order)
        .order_by(Region.sort_order)
        .all()
    )
    category_rows = db.query(Place.category, func.count(Place.id)).group_by(Place.category).all()
    return {
        "user_count": db.query(func.count(User.id)).scalar() or 0,
        "place_count": db.query(func.count(Place.id)).scalar() or 0,
        "region_count": db.query(func.count(Region.id)).scalar() or 0,
        "trip_stop_count": db.query(func.count(TripStop.id)).scalar() or 0,
        "favorite_count": db.query(func.count(Favorite.id)).scalar() or 0,
        "condition_count": db.query(func.count(Place.id)).filter(
            or_(
                Place.weather_sensitive.is_(True),
                Place.tide_sensitive.is_(True),
                Place.ferry_sensitive.is_(True),
                Place.booking_required.is_(True),
            )
        ).scalar() or 0,
        "chat_message_count": db.query(func.count(ChatMessage.id)).scalar() or 0,
        "batch_run_count": db.query(func.count(BatchRun.id)).scalar() or 0,
        "regions": [
            {"id": row[0], "name": row[1], "island": row[2], "place_count": row[3]}
            for row in region_rows
        ],
        "categories": {row[0]: row[1] for row in category_rows},
    }


@app.get("/api/admin/batch/runs", response_model=list[BatchRunOut])
def admin_batch_runs(
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[BatchRunOut]:
    return [BatchRunOut.model_validate(row) for row in db.query(BatchRun).order_by(BatchRun.id.desc()).limit(30).all()]


@app.post("/api/admin/batch/run", response_model=BatchRunOut)
def admin_run_batch(
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> BatchRunOut:
    return BatchRunOut.model_validate(run_batch(db, trigger="manual"))


@app.get("/api/admin/places", response_model=list[PlaceOut])
def admin_places(
    q: str = "",
    region_id: int | None = Query(default=None, gt=0),
    include_merged: bool = False,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> list[PlaceOut]:
    query = db.query(Place).options(joinedload(Place.region))
    if not include_merged:
        query = query.filter(Place.merged_into_id.is_(None))
    if region_id:
        query = query.filter(Place.region_id == region_id)
    if q.strip():
        value = "%" + q.strip() + "%"
        query = query.filter(or_(Place.title.ilike(value), Place.local_name.ilike(value), Place.area.ilike(value)))
    favorite_ids = user_favorite_ids(db, admin.id)
    return [place_out(row, favorite_ids) for row in query.order_by(Place.region_id, Place.title).all()]


@app.patch("/api/admin/places/{place_id}", response_model=PlaceOut)
def admin_update_place(
    place_id: int,
    body: AdminPlaceUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> PlaceOut:
    row = load_place(db, place_id, for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    values = body.model_dump(exclude_unset=True)
    if "region_id" in values and db.get(Region, values["region_id"]) is None:
        raise HTTPException(status_code=404, detail="권역을 찾을 수 없습니다")
    if "tags" in values:
        values["tags"] = ",".join(dict.fromkeys(item.strip() for item in values["tags"] if item.strip()))
    target_region = db.get(Region, values.get("region_id", row.region_id))
    target_lat = values.get("lat", row.lat)
    target_lng = values.get("lng", row.lng)
    if target_region is None or not (
        target_region.south <= target_lat <= target_region.north
        and target_region.west <= target_lng <= target_region.east
    ):
        raise HTTPException(status_code=422, detail="선택한 권역의 지도 범위 밖입니다")
    apply_place_update_with_audit(
        db,
        place=row,
        actor_id=admin.id,
        values=values,
        summary="관리자가 장소 정보를 수정했습니다",
    )
    db.commit()
    return place_out(load_place(db, row.id), user_favorite_ids(db, admin.id))


@app.delete("/api/admin/places/{place_id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_delete_place(
    place_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> Response:
    row = load_place(db, place_id, for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    dependency = place_deletion_dependency(db, row.id)
    if dependency:
        raise HTTPException(
            status_code=409,
            detail=f"{dependency}에서 사용 중인 장소는 삭제할 수 없습니다. 연결 데이터를 먼저 정리해 주세요",
        )
    record_place_deletion(db, place=row, actor_id=admin.id, summary="관리자가 장소를 삭제했습니다")
    db.delete(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="다른 여행 데이터에서 사용 중인 장소는 삭제할 수 없습니다",
        ) from exc
    return Response(status_code=204)


@app.get("/api/trip", response_model=list[TripStopOut])
def trip(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[TripStopOut]:
    favorite_ids = user_favorite_ids(db, user.id)
    rows = (
        db.query(TripStop)
        .options(joinedload(TripStop.place).joinedload(Place.region))
        .filter(TripStop.user_id == user.id)
        .order_by(TripStop.day_number, TripStop.sort_order, TripStop.id)
        .all()
    )
    return [
        TripStopOut(
            id=row.id,
            day_number=row.day_number,
            sort_order=row.sort_order,
            note=row.note,
            place=place_out(row.place, favorite_ids),
        )
        for row in rows
    ]


@app.post("/api/trip", response_model=TripStopOut, status_code=status.HTTP_201_CREATED)
def add_trip_stop(
    body: TripStopCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TripStopOut:
    place = load_place(db, body.place_id, for_update=True)
    if place is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    existing = db.query(TripStop).filter(
        TripStop.user_id == user.id,
        TripStop.place_id == body.place_id,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="이미 여행에 담긴 장소입니다")
    max_order = max(
        [
            row[0]
            for row in db.query(TripStop.sort_order).filter(
                TripStop.user_id == user.id,
                TripStop.day_number == body.day_number,
            ).all()
        ]
        or [0]
    )
    row = TripStop(
        user_id=user.id,
        place_id=body.place_id,
        day_number=body.day_number,
        sort_order=max_order + 10,
        note=body.note,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return TripStopOut(
        id=row.id,
        day_number=row.day_number,
        sort_order=row.sort_order,
        note=row.note,
        place=place_out(place, user_favorite_ids(db, user.id)),
    )


@app.patch("/api/trip/{stop_id}", response_model=TripStopOut)
def update_trip_stop(
    stop_id: int,
    body: TripStopUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TripStopOut:
    row = db.query(TripStop).filter(TripStop.id == stop_id, TripStop.user_id == user.id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="일정 항목을 찾을 수 없습니다")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    db.commit()
    place = load_place(db, row.place_id)
    return TripStopOut(
        id=row.id,
        day_number=row.day_number,
        sort_order=row.sort_order,
        note=row.note,
        place=place_out(place, user_favorite_ids(db, user.id)),
    )


@app.delete("/api/trip/{stop_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_trip_stop(
    stop_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    row = db.query(TripStop).filter(TripStop.id == stop_id, TripStop.user_id == user.id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="일정 항목을 찾을 수 없습니다")
    db.delete(row)
    db.commit()
    return Response(status_code=204)


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.is_dir():
    assets = STATIC_DIR / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/")
    def spa_index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str) -> FileResponse:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = STATIC_DIR / full_path
        return FileResponse(candidate if candidate.is_file() else STATIC_DIR / "index.html")
